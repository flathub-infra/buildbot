# -*- python -*-
# ex: set filetype=python:

from future.utils import iteritems
from future.utils import string_types
from buildbot.interfaces import ITriggerableScheduler
from buildbot.process.build import Build
from buildbot.plugins import *
from buildbot.process import logobserver
from buildbot.process import remotecommand
from buildbot.process.buildstep import FAILURE
from buildbot.process.buildstep import SUCCESS
from buildbot.process.buildstep import CANCELLED
from buildbot.process.buildstep import EXCEPTION
from buildbot.process.buildstep import BuildStep
from buildbot.process.properties import Properties
from buildbot.process.properties import Property
from buildbot.data import resultspec
from buildbot.util import epoch2datetime
from buildbot.util import unicode2bytes
from buildbot import locks
from twisted.python import log
from twisted.python import util as pyutil
from twisted.internet import defer
import buildbot
import subprocess, re, json
import os
import os.path
from buildbot.steps.worker import CompositeStepMixin
from buildbot.www.hooks.github import GitHubEventHandler
from buildbot.www import authz
from datetime import timedelta
from datetime import datetime
from dateutil.parser import parse as dateparse
from zope.interface import implementer
from buildbot.interfaces import IConfigured
from buildbot.www.hooks import gitlab as gitlabhooks
from buildbot.worker.local import LocalWorker
from buildbot.worker.base import Worker

import requests
import txrequests
import logging
from buildbot.flathub_builds import Builds
import sys
import yaml

# Global configuration
config = None

# Global build config
builds = None

# Worker config generated from builders.json
flathub_workers = []                    # All workers configured
flathub_worker_names = []               # All workers configured
flathub_arches = []                     # Calculated from the arch key on the builders
flathub_arch_workers = {}               # Map from arch to list of worker names from flathub_workers
flathub_download_sources_workers = []   # Worker names for source downloaders

# These keeps track of subset tokens for uploads by the workers
flathub_upload_tokens = {}

flatmgr_client = ["flatpak", "run", "org.flatpak.flat-manager-client"]

adminsGithubGroup='flathub'

flatpak_update_lock = util.WorkerLock("flatpak-update")
# This lock is taken in a shared mode for builds, but in exclusive mode when doing
# global worker operations like cleanup
flatpak_worker_lock = util.WorkerLock("flatpak-worker-lock", maxCount=1000)

# Certain repo manager jobs are serialized anyway (commit, publish), so we
# only work on one at a time
repo_manager_lock = util.MasterLock("repo-manager")

# We only want to run one periodic job at a time
periodic_lock = util.MasterLock("periodic-lock")

# This is an in-memory mapping from flat-manager update repo job id to buildbot buildrequestid
# I is used to avoid multiple builds for the same job, but is technically "optional", so ok
# to forget at restart.
flathub_update_jobs_cache = {}

##### Properties

# These properties are set in the main build and inherited to sub-builds
inherited_properties=[
    # From Change
    'flathub_id',               # The id of the build (always correct)
    'flathub_branch',           # Optional branch of the id (like 3.30 in org.gnome.Sdk/3.30)
    'flathub_official_build',   # True if this is an official (non-test) build
    'flathub_custom_buildcmd',  # True if a ./build.sh should be used instead of flatpak-builder
    'flathub_extra_prefixes',   # Extra flatpak id prefixes created by this build
    # Set by CreateRepoBuildStep
    'flathub_repo_id',          # This is the id of the build in the repo-manager
    # Optionally from Change, or from FlathubPropertiesStep
    'flathub_arches',           # Arches to build on ('flathub_arch' is also set on the sub-builds)
    # Set by FlathubPropertiesStep
    'flathub_name',             # id[/branch]
    'flathub_default_branch',   # The default branch for the build (always set, might now be correct for non-apps, i.e. be stable for icon theme 1.0 branch)
    'flathub_subject',          # Subject to use for commit
    'flathub_buildnumber',      # Buildnumber of the main build (used to order builds)
    'flathub_manifest',         # Filename of the manifest to build
    'flathub_config',           # parsed version of flathub.json
    # Other
    'flathub_issue_url',        # Set if from isssue/pr
    'flathub_untrusted',        # Set if from isssue/pr
    'reason'
    ]
# Other properties
#   'flathub_arch'              # Set by the per-arch builders

# In addition to normal properties we have some custom db fields in the build database table
# so that these can be used for efficient queries too:
# Set during FlathubPropertiesStep
#  flathub_name
#  flathub_build_type
# Set during CreateRepoBuildStep and updated in SetRepoStateStep
#  flathub_repo_id
#  flathub_repo_status (updated over time via triggered child builds)

GITHUB_API_BASE="https://api.github.com"

####### SETUP

class FlathubConfig():
    def __init__(self):
        f = open('config.json', 'r')
        # This strips /* comments */
        config_data = json.loads(re.sub(r'/\*.*?\*/', '', f.read(), flags=re.DOTALL))

        def getConfig(config_data, name, default=""):
            return config_data.get(name, default)

        def getConfigv(config_data, name, default=[]):
            return config_data.get(name, default)

        self.repo_manager_token = getConfig(config_data, 'repo-manager-token')
        self.repo_manager_uri = getConfig(config_data, 'repo-manager-uri')
        self.repo_manager_dl_uri = getConfig(config_data, 'repo-manager-dl-uri', self.repo_manager_uri)
        self.buildbot_port = getConfig(config_data, 'buildbot-port', 8010)
        self.num_master_workers = getConfig(config_data, 'num-master-workers', 4)
        self.buildbot_uri = getConfig(config_data, 'buildbot-uri')
        self.upstream_repo = getConfig(config_data, 'flathub-repo')
        self.upstream_beta_repo = getConfig(config_data, 'flathub-beta-repo')
        self.upstream_sources_uri = getConfig(config_data, 'flathub-sources-uri', os.path.join (self.upstream_repo, "sources" ))
        self.upstream_sources_path = getConfig(config_data, 'flathub-sources-path')
        self.admin_password = getConfig(config_data, 'admin-password')
        self.github_auth_client = getConfig(config_data, 'github-auth-client')
        self.github_auth_secret = getConfig(config_data, 'github-auth-secret')
        self.github_change_secret = getConfig(config_data, 'github-change-secret')
        self.gitlab_change_secret = getConfig(config_data, 'gitlab-change-secret')
        self.github_api_token = getConfig(config_data, 'github-api-token')
        self.db_uri = getConfig(config_data, 'db-uri', "sqlite:///state.sqlite")
        self.keep_test_build_days = getConfig(config_data, 'keep-test-build-days', 5)
        self.publish_default_delay_hours = getConfig(config_data, 'publish-default-delay-hours', 24)
        self.comment_prefix_text = getConfig(config_data, 'comment-prefix-test', None)
        self.disable_status_updates = getConfig(config_data, 'disable-status-updates', False)
        self.bot_name = getConfig(config_data, 'bot-name', 'bot')
        self.tag_mapping = getConfig(config_data, 'tag-mapping', {})


def reload_builds():
    global builds_mtime
    global builds
    try:
        new_builds_mtime = os.path.getmtime('builds.json')
        if new_builds_mtime != builds_mtime:
            log.msg("Reloading builds.json")
            new_builds = Builds('builds.json')
            builds_mtime = new_builds_mtime
            builds = new_builds
    except BaseException as e:
        msg = str(e)
        log.msg("WARNING: Failed to reload builds.json: %s" % (msg))
        return False
    return True


def load_config():
    # First load all json file to catch any errors
    # before we modify global state
    new_config = FlathubConfig()
    new_builds_mtime = os.path.getmtime('builds.json')
    new_builds = Builds('builds.json')

    f = open('builders.json', 'r')
    # This strips /* comments */
    worker_config = json.loads(re.sub(r'/\*.*?\*/', '', f.read(), flags=re.DOTALL))

    # Json parsing succeeded, now change global config

    global config
    global builds_mtime
    global builds
    global flathub_workers
    global flathub_worker_names
    global flathub_arches
    global flathub_arch_workers
    global flathub_download_sources_workers

    config = new_config
    builds_mtime = new_builds_mtime
    builds = new_builds
    flathub_workers = []
    flathub_worker_names = []
    flathub_arches = []
    flathub_arch_workers = {}
    flathub_download_sources_workers = []

    for w in worker_config.keys():
        wc = worker_config[w]
        passwd = wc['password']
        max_builds = 1
        if 'max-builds' in wc:
            max_builds = wc['max-builds']
        wk = Worker(w, passwd, max_builds=max_builds)
        wk.flathub_classes = []
        if 'classes' in wc:
            wk.flathub_classes = wc['classes']
        flathub_workers.append (wk)
        flathub_worker_names.append(wk.name)
        if 'arches' in wc:
            for a in wc['arches']:
                if not a in flathub_arches:
                    flathub_arches.append(a)
                    flathub_arch_workers[a] = []
                flathub_arch_workers[a].append(w)
        if 'download-sources' in wc:
            flathub_download_sources_workers.append(w)

####### Custom renderers and helper functions

# Buildbot does unique builds per build + worker, so if we want a single worker
# to build multiple apps of the same arch, then we need to have multiple build
# ids for each arch, so we append a number which we calculate from the build id
def num_builders_per_arch():
    return config.num_master_workers

@util.renderer
def computeUploadToken(props):
    build_id = props.getProperty ('flathub_repo_id')
    return flathub_upload_tokens[build_id]

@util.renderer
def computeBuildId(props):
    return props.getBuild().buildid

@util.renderer
def computeMasterBaseDir(props):
    return props.master.basedir

@util.renderer
def computeStatusContext(props):
    buildername = props.getProperty ('buildername')
    if buildername == "download-sources":
        return "download-sources"
    arch = props.getProperty ('flathub_arch')
    return "builds/%s" % (arch)

@util.renderer
def computeExtraIdArgs(props):
    extra_ids = []
    built_tags = props.getProperty ('flathub_built_tags')
    for tag in config.tag_mapping:
        if tag in built_tags:
            extra_ids.append(config.tag_mapping[tag])
    return list(map(lambda s: "--extra-id=%s" % (s), extra_ids))

@util.renderer
def computeCommitArgs(props):
    flathub_config = props.getProperty ('flathub_config', {})
    eol = flathub_config.get("end-of-life", None)
    eol_rebase = flathub_config.get("end-of-life-rebase", None)
    args = [*flatmgr_client, 'commit', '--wait']
    if eol:
        args = args + [ "--end-of-life=%s" % (eol) ]
    if eol_rebase:
        args = args + [ "--end-of-life-rebase=%s" % (eol_rebase) ]
    args = args + [util.Interpolate("%(kw:url)s/api/v1/build/%(prop:flathub_repo_id)s", url=config.repo_manager_uri)]
    return args

# This is a bit weird, but we generate the builder name from the build number to spread them around
# on the various builders, or we would only ever build one at a time, because each named builder
# is serialized
def getArchBuilderName(arch, buildnr):
    b = buildnr % num_builders_per_arch()
    if b == 0:
        build_extra = ""
    else:
        build_extra = "-%s" % b

    return "build-" + arch + build_extra

@util.renderer
def computeBuildArches(props):
    buildnr = props.getProperty ('flathub_buildnumber', 0)
    return list(map(lambda arch: getArchBuilderName(arch, buildnr), props.getProperty("flathub_arches", [])))

def hide_on_success(results, s):
    return results==buildbot.process.results.SUCCESS

def hide_on_skipped(results, s):
    return results==buildbot.process.results.SKIPPED

def hide_on_success_or_skipped(results, s):
    return results==buildbot.process.results.SUCCESS or results==buildbot.process.results.SKIPPED

# Triggers stop working on shutdown, so don't use them then
def do_if_failed_except_shutdown(step):
    return not step.master.botmaster.shuttingDown and not step.build.results == SUCCESS

# Official builds are master branch on the canonical flathub git repo
def build_is_official(step):
    return step.build.getProperty ('flathub_official_build', False)

def shellArgOptional(commands):
    return util.ShellArg(logfile='stdio', command=commands)

def shellArg(commands):
    return util.ShellArg(logfile='stdio', haltOnFailure=True, command=commands)

def should_skip_icons_check(step):
    skip_icons_check = step.build.getProperty('flathub_config', {}).get('skip-icons-check')
    skip_appstream_check = step.build.getProperty('flathub_config', {}).get('skip-appstream-check')

    return not (skip_icons_check or skip_appstream_check)

class TitleObserver(logobserver.LogObserver):
    title_re = re.compile (r"\x1b]2;[\s\w!/\\#$%&'*+-.^_`|~:~]*\x07")

    def __init__(self):
        pass

    def gotData(self, stream, data):
        if data:
            for m in self.title_re.findall(data):
                title = m[4:-1]
                self.step.setTitle(title)

def asciiBasename(s):
    return asciiize(os.path.basename(s))

def inherit_properties(propnames):
    res = {}
    for name in propnames:
        res[name] = util.Property(name)
    return res

####### Custom steps

class UpdateConfig(steps.BuildStep):
    parms = steps.BuildStep.parms + []
    description = 'Updating Config'
    descriptionDone = 'Updated'

    def __init__(self, **kwargs):
        for p in self.__class__.parms:
            if p in kwargs:
                setattr(self, p, kwargs.pop(p))

        steps.BuildStep.__init__(self, **kwargs)

    @defer.inlineCallbacks
    def run(self):
        log = yield self.addLog('log')
        if not reload_builds ():
            log.addStderr('Failed to update the builds.json config, ping the admins')
            defer.returnValue(FAILURE)
            return

        log.finish()

        defer.returnValue(SUCCESS)

class MaybeAddSteps(steps.BuildStep):
    parms = steps.BuildStep.parms + ['steps']
    steps = None
    predicate = None

    def __init__(self, predicate, **kwargs):
        for p in self.__class__.parms:
            if p in kwargs:
                setattr(self, p, kwargs.pop(p))

        steps.BuildStep.__init__(self, **kwargs)
        self.predicate = predicate

    def run(self):
        if self.predicate(self.build):
            self.build.addStepsAfterCurrentStep(self.steps)
        return buildbot.process.results.SUCCESS

class RepoRequestStep(steps.BuildStep):
    name = 'RepoRequestStep'
    description = 'Requesting'
    descriptionDone = 'Requested'
    renderables = ["method", "url", "headers", "json"]

    def __init__(self, path, **kwargs):
        if txrequests is None:
            raise Exception(
                "Need to install txrequest to use this step:\n\n pip install txrequests")

        self.method = 'POST'
        self.url = config.repo_manager_uri + '/api/v1' + path
        self.json = None
        self.headers = {
            'Authorization': util.Interpolate('Bearer %s' % (config.repo_manager_token))
        }

        steps.BuildStep.__init__(self, **kwargs)

    def start(self):
        self.initRequest()
        d = self.doRequest()
        d.addErrback(self.failed)

    def _sleep(self, delay):
        d = defer.Deferred()
        reactor.callLater(delay, d.callback, 1)
        return d

    @defer.inlineCallbacks
    def doRequest(self):
        # We use a new session each time, to isolate the requests. This avoids
        # e.g. getting ECONNRESET if the server shut down the session.

        requestkwargs = {
            'method': self.method,
            'url': self.url,
            'headers': self.headers,
            'json': self.json
        }

        log = yield self.addLog('log')
        retries = 0
        while True:
            log.addHeader('Performing %s request to %s\n' %
                          (self.method, self.url))
            session = txrequests.Session()
            try:
                response = yield session.request(**requestkwargs)
            except requests.exceptions.ConnectionError as e:
                session.close()
                log.addStderr(
                    'An exception occurred while performing the request: %s' % e)
                self.finished(FAILURE)
                return
            session.close()

            if response.status_code == requests.codes.service_unavailable:
                retries = retries + 1
                if retries < 5:
                    log.addStderr('Got 503, retrying in 10 sec...')
                    yield self._sleep(self.mirror_sync_sleep)
                    continue
                else:
                    log.addStderr('Got 503 again, giving up...')

            break

        if response.status_code == requests.codes.ok:
            resp = json.loads(response.text)
            yield self.gotOk(resp, log)

        log.finish()

        self.descriptionDone = ["Status code: %d" % response.status_code]
        if (response.status_code < 400):
            self.finished(SUCCESS)
        else:
            self.finished(FAILURE)

    def initRequest(self):
        pass

    def gotOk(self, response, log):
        pass

class CreateRepoBuildStep(RepoRequestStep):
    name = 'CreateRepoBuildStep'

    def __init__(self, **kwargs):
        RepoRequestStep.__init__(self, '/build', haltOnFailure=True, **kwargs)

    def initRequest(self):
        props = self.build.properties
        repo = 'stable'
        fp_branch = props.getProperty("flathub_branch")
        if fp_branch == "beta":
            repo = "beta"
        self.json = { 'repo': repo }

    @defer.inlineCallbacks
    def gotOk(self, response, log):
        log.addStdout("response: " + str(response))
        repo_id = response['id'];
        yield self.master.data.updates.setBuildFlathubRepoId(self.build.buildid, repo_id)
        yield self.master.data.updates.setBuildFlathubRepoStatus(self.build.buildid, 0)
        # We need this in the properties too, so we can easily read it out
        self.build.setProperty('flathub_repo_id', repo_id , 'CreateRepoBuildStep', runtime=True)


# If the app has dashes in the last element, like "org.foo.with-dash", then
# we will also have to accept ids like org.foo.with_dash.Debug, because
# flatpak does this because dashes are only allowed in the last part.
def convert_dashes_in_id(id):
    idx = id.rfind(".")
    if idx == -1:
        return id
    return id[:idx] + id[idx:].replace("-", "_")

class CreateUploadToken(RepoRequestStep):
    name = 'CreateUploadToken'

    def __init__(self, **kwargs):
        RepoRequestStep.__init__(self, '/token_subset', haltOnFailure=True, **kwargs)

    def initRequest(self):
        props = self.build.properties
        id = props.getProperty("flathub_id")
        prefix = [id]
        id2 = convert_dashes_in_id(id)
        if id != id2:
            prefix.append(id2)

        extra_properties = props.getProperty("flathub_extra_prefixes")
        if extra_properties:
            prefix.extend(extra_properties)

        self.json = {
            "name": "upload",
            "sub": "build/%s" % props.getProperty("flathub_repo_id"),
            "scope": ["upload"],
            "prefix": prefix,
            "duration": 60*60*24
        }

    def gotOk(self, response, log):
        build_id = self.build.getProperty ('flathub_repo_id')
        flathub_upload_tokens[build_id] = response['token']

class SetPublishJobStep(steps.BuildStep):
    def __init__(self, buildid=None, **kwargs):
        self.buildid=buildid
        steps.BuildStep.__init__(self, haltOnFailure=True, **kwargs)

    def start(self):
        d = self.doSet()
        d.addErrback(self.failed)

    @defer.inlineCallbacks
    def doSet(self):
        if self.buildid is not None:
            buildid = self.buildid
        else:
            buildid = self.build.getProperty ('flathub_orig_buildid', None)
        yield self.master.data.updates.setBuildProperty(buildid, "flathub_publish_buildid", self.build.buildid, 'SetPublishJobStep')
        self.finished(SUCCESS)


class HandleUpdateRepoStep(steps.BuildStep, CompositeStepMixin):
    def __init__(self, buildid=None, **kwargs):
        self.buildid=buildid
        steps.BuildStep.__init__(self, haltOnFailure=True, **kwargs)
        self.logEnviron = False

    def getSchedulerByName(self, name):
        # we use the fact that scheduler_manager is a multiservice, with schedulers as childs
        # this allow to quickly find schedulers instance by name
        schedulers = self.master.scheduler_manager.namedServices
        if name not in schedulers:
            raise ValueError("unknown triggered scheduler: %r" % (name,))
        sch = schedulers[name]
        if not ITriggerableScheduler.providedBy(sch):
            raise ValueError(
                "triggered scheduler is not ITriggerableScheduler: %r" % (name,))
        return sch

    @defer.inlineCallbacks
    def run(self):
        output = {}
        content = yield self.getFileContentFromWorker("output.json")
        if content == None:
            return defer.returnValue(SUCCESS)

        output = json.loads(content)

        log = yield self.addLog('log')
        log.addStdout("response: " + str(output) + "\n")
        update_repo_job = output.get("result", {}).get("results", {}).get("update-repo-job", None)

        log.addStdout("update job: " + str(update_repo_job) + "\n")

        buildrequestid = flathub_update_jobs_cache.get(update_repo_job, None)
        if not buildrequestid:
            sch = self.getSchedulerByName("update-repo")
            set_properties = Properties()
            set_properties.update({ "flathub_update_job_id" : update_repo_job }, "HandleUpdateRepoStep")

            idsDeferred, resultsDeferred = sch.trigger(
                waited_for=False, sourcestamps=[],
                set_props=set_properties,
                parent_buildid=None,
                parent_relationship=None
            )

            brids = {}
            try:
                bsid, brids = yield idsDeferred
            except Exception as e:
                yield self.addLogWithException(e)
                return defer.returnValue(EXCEPTION)

            buildrequestid = list(brids.values())[0]
            log.addStdout("brid: " + str(buildrequestid) + "\n")
            flathub_update_jobs_cache[update_repo_job] = buildrequestid

        self.build.setProperty('flathub_update_repo_buildreq', buildrequestid , 'HandleUpdateRepoStep', runtime=True)

        if self.buildid is not None:
            buildid = self.buildid
        else:
            buildid = self.build.getProperty ('flathub_orig_buildid', None)

        yield self.master.data.updates.setBuildProperty(buildid, "flathub_update_repo_buildreq", buildrequestid, 'HandleUpdateRepoStep')

        log.finish()
        defer.returnValue(SUCCESS)

class SetRepoStateStep(steps.BuildStep):
    def __init__(self, value, buildid=None, **kwargs):
        self.value=value
        self.buildid=buildid
        steps.BuildStep.__init__(self, haltOnFailure=True, **kwargs)

    def start(self):
        d = self.doClear()
        d.addErrback(self.failed)

    @defer.inlineCallbacks
    def doClear(self):
        if self.buildid is not None:
            buildid = self.buildid
        else:
            buildid = self.build.getProperty ('flathub_orig_buildid', None)
            if not buildid:
                buildid = self.build.buildid
        yield self.master.data.updates.setBuildFlathubRepoStatus(buildid, self.value)
        self.finished(SUCCESS)


class ClearUploadToken(steps.BuildStep):
    def __init__(self, **kwargs):
        steps.BuildStep.__init__(self, haltOnFailure = True, **kwargs)

    def run(self):
        build_id = self.build.getProperty ('flathub_repo_id', None)
        if build_id is not None and build_id in flathub_upload_tokens:
            del flathub_upload_tokens[build_id]
        return buildbot.process.results.SUCCESS

class CheckNewerBuildStep(steps.BuildStep):
    def __init__(self, **kwargs):
        steps.BuildStep.__init__(self, haltOnFailure=True, **kwargs)

    def start(self):
        d = self.doGet()
        d.addErrback(self.failed)

    @defer.inlineCallbacks
    def doGet(self):
        build = self.build
        build_name = build.getProperty ('flathub_name', "")
        builderid = yield build.getBuilderId()
        newer_published_builds = yield self.master.data.get(('builds',),
                                                            order=['-number'],
                                                            filters=[resultspec.Filter('builderid', 'eq', [builderid]),
                                                                     resultspec.Filter('flathub_name', 'eq', [build_name]),
                                                                     resultspec.Filter('flathub_build_type', 'eq', [1]),
                                                                     resultspec.Filter('number', 'gt', [build.number]),
                                                                     resultspec.Filter('flathub_repo_status', 'eq', [2]),
                                                            ])
        if len(newer_published_builds) > 0:
            self.descriptionDone = ["Found newer published build"]
            self.finished(FAILURE)
        else:
            self.finished(SUCCESS)

class PurgeOldBuildsStep(steps.BuildStep):
    def __init__(self, **kwargs):
        steps.BuildStep.__init__(self, haltOnFailure=True, **kwargs)

    def start(self):
        d = self.doGet()
        d.addErrback(self.failed)

    @defer.inlineCallbacks
    def doGet(self):
        orig_buildid = self.build.getProperty ('flathub_orig_buildid')
        build = yield self.master.db.builds.getBuild(orig_buildid)
        # Find all older (<num), official completed builds with the same name
        # that are not published or deleted and delete them:
        builds = yield self.master.data.get(('builds',),
                                            order=['-number'],
                                            filters=[resultspec.Filter('complete', 'eq', [True]),
                                                     resultspec.Filter('builderid', 'eq', [build['builderid']]),
                                                     resultspec.Filter('flathub_name', 'eq', [build['flathub_name']]),
                                                     resultspec.Filter('flathub_build_type', 'eq', [1]),
                                                     resultspec.Filter('number', 'lt', [build['number']]),
                                                     resultspec.Filter('flathub_repo_status', 'le', [1]) ])
        for b in builds:
            self.build.addStepsAfterCurrentStep([
                steps.ShellSequence(name='Purging old builds %d' % (b['number']),
                                    logEnviron=False,
                                    haltOnFailure=False,
                                    usePTY=True,
                                    env={"REPO_TOKEN": config.repo_manager_token},
                                    commands=[
                                        shellArg([*flatmgr_client, 'purge',
                                                  "%s/api/v1/build/%s" % (config.repo_manager_uri, b['flathub_repo_id'])])
                                    ]),
                SetRepoStateStep(3, buildid=b['buildid'], name='Marking old build id %d as deleted' % b['number']),
            ])
        self.finished(SUCCESS)

class PeriodicPurgeStep(steps.BuildStep):
    def __init__(self, **kwargs):
        steps.BuildStep.__init__(self, haltOnFailure=True, **kwargs)

    def start(self):
        d = self.doGet()
        d.addErrback(self.failed)

    @defer.inlineCallbacks
    def doGet(self):
        builderid = yield self.build.builder.getBuilderIdForName('Builds')
        # Find all older (<num), official completed builds with the same name
        # that are not published or deleted and delete them:
        builds = yield self.master.data.get(('builds',),
                                            order=['-number'],
                                            filters=[resultspec.Filter('complete', 'eq', [True]),
                                                     resultspec.Filter('builderid', 'eq', [builderid]),
                                                     resultspec.Filter('flathub_repo_status', 'le', [1]) ])
        max_test_age = timedelta(config.keep_test_build_days)
        published={}
        for b in builds:
            age=epoch2datetime(self.master.reactor.seconds()) - b['complete_at']
            if b['flathub_build_type'] == 0:
                # Test build
                if age > max_test_age:
                    self.build.addStepsAfterCurrentStep([
                        steps.Trigger(name='Deleting old test build',
                                      schedulerNames=['purge'],
                                      waitForFinish=True,
                                      set_properties={
                                          'flathub_repo_id' : b['flathub_repo_id'],
                                          'flathub_orig_buildid': b['buildid'],
                                      }),
                    ])
            if b['flathub_build_type'] == 1:
                # Official build
                name = b['flathub_name']
                if not name in published:
                    props = yield self.master.db.builds.getBuildProperties(b['buildid'])
                    fh_config = props.get(u'flathub_config', ({}))[0]
                    max_age_hours = fh_config.get(u'publish-delay-hours', config.publish_default_delay_hours)
                    max_age = timedelta(0, max_age_hours*60*60)
                    if age > max_age:
                        if b['flathub_repo_status'] == 1:
                            published[name] = True # Don't publish earlier version, the will be purged by the publish
                            step = steps.Trigger(name='Auto-publishing old build %d' % b['number'],
                                                 schedulerNames=['publish'],
                                                 waitForFinish=True,
                                                 set_properties={
                                                     'flathub_repo_id' : b['flathub_repo_id'],
                                                     'flathub_orig_buildid': b['buildid'],
                                                     'flathub_name': b['flathub_name'],
                                                     'flathub_buildnumber': b['number'],
                                                 })
                        else: # Some leftover non-commited job? error?
                            steps.Trigger(name='Deleting old broken build %d' % b['number'],
                                          schedulerNames=['purge'],
                                          waitForFinish=True,
                                          set_properties={
                                              'flathub_repo_id' : b['flathub_repo_id'],
                                              'flathub_orig_buildid': b['buildid'],
                                          })
                        self.build.addStepsAfterCurrentStep([step])
        self.finished(SUCCESS)

@defer.inlineCallbacks
def githubApiRequest(path):
    session = requests.Session()
    res = yield session.get(GITHUB_API_BASE + path,
                            headers={
                                "Authorization": "token  " + config.github_api_token
                            })
    defer.returnValue(res)

@defer.inlineCallbacks
def githubApiPostComment(issue_url, comment):
    session = requests.Session()
    if config.comment_prefix_text:
        comment = config.comment_prefix_text + "\n\n" + comment
    res = yield session.post(issue_url + "/comments",
                             headers={
                                 "Authorization": "token  " + config.github_api_token
                             },
                             json= {
                                 'body': comment
                             })
    defer.returnValue(res)

@defer.inlineCallbacks
def githubApiAssertUserMaintainsRepo(repo_url, userDetails):
    username = userDetails.get('username', None)
    if config.github_auth_client == '' and config.admin_password != '':
        allowed = username == "admin"
    else:
        groups = userDetails.get('groups', [])
        allowed = adminsGithubGroup in groups
    if not allowed and username != None and config.github_api_token != "" and repo_url.startswith("https://github.com/flathub/"):
        basename = os.path.basename(repo_url)
        if basename.endswith(".git"):
            basename = basename[:-4]
        response = yield githubApiRequest("/repos/flathub/%s/collaborators/%s" % (basename, username))
        if response.ok:
            allowed = True
        elif response.status_code != 404:
            log.msg("WARNING: Unexpected repsonse from %s: %d" % (url, response.status_code))
    if not allowed:
        error_msg = unicode2bytes(
            "You need commit permissions to %s or flathub admin status" % repo_url)
        raise authz.Forbidden(error_msg)
    defer.returnValue(None)

@implementer(IConfigured)
class FlathubAuthz(authz.Authz):
    def __init__(self, allowRules=None, roleMatchers=None, stringsMatcher=authz.fnmatchStrMatcher):
        authz.Authz.__init__(self, allowRules=allowRules, roleMatchers=roleMatchers, stringsMatcher=stringsMatcher)

    @defer.inlineCallbacks
    def assertUserMaintainsBuild(self, build_id, userDetails):
        build = yield self.master.data.get(("builds", build_id))
        buildrequest = yield self.master.data.get(('buildrequests', build['buildrequestid']))
        buildset = yield self.master.data.get(("buildsets", buildrequest['buildsetid']))
        repo = buildset['sourcestamps'][0]['repository']
        yield githubApiAssertUserMaintainsRepo(repo, userDetails)
        defer.returnValue(None)

    @defer.inlineCallbacks
    def assertUserAllowed(self, ep, action, options, userDetails):
        if len(ep) == 2 and ep[0] == 'builds' and (action == u'publish' or action == u'delete' or action == u'rebuild' or action == u'stop'):
            yield self.assertUserMaintainsBuild(ep[1], userDetails)
            defer.returnValue(None)

        if len(ep) == 2 and ep[0] == 'forceschedulers' and ep[1] == 'build-app' and action == u'force':
            git_repo_uri = options[u'Override git repo_repository']
            git_branch = options[u'Override git repo_branch']
            buildname = options[u'buildname']
            data = builds.lookup_by_git_and_name(git_repo_uri, git_branch, buildname)
            flathub_git_repo = data.get_flathub_repo_uri()
            yield githubApiAssertUserMaintainsRepo(flathub_git_repo, userDetails)
            defer.returnValue(None)

        yield authz.Authz.assertUserAllowed(self, ep, action, options, userDetails)

class AppParameter(util.CodebaseParameter):

    """A parameter whose result is a codebase specification instead of a property"""
    type = util.CodebaseParameter.type
    codebase = ''

    def __init__(self,
                 codebase,
                 name=None,
                 label=None,
                 **kwargs):

        util.CodebaseParameter.__init__(self, name=name, label=label,
                                        codebase=codebase,
                                        branch=util.StringParameter(name="branch", label='branch:', default=""),
                                        revision=util.FixedParameter(name="revision", default=""),
                                        repository=util.StringParameter(name="repository", label='repo uri:', default=""),
                                        project=util.FixedParameter(name="project", default=""),
                                        **kwargs)

    def createSourcestamp(self, properties, kwargs):
        cb = kwargs.get(self.fullName, [])
        branch = cb['branch']
        buildname = kwargs.get('buildname', None)[0]
        force_arches = kwargs.get('force-arches', None)[0]
        force_test = kwargs.get('force-test-build', None)[0]
        repo_uri = cb['repository']
        data = builds.lookup_by_git_and_name(repo_uri, branch, buildname)
        change = data.get_change(force_test=force_test, force_arches=force_arches)
        properties.update(change['properties'])
        return {
            'repository': change['repository'],
            'branch': change['branch'],
            'revision': '',
        }

def create_build_app_force_scheduler():
    return schedulers.ForceScheduler(
        name="build-app",
        buttonName="Start build",
        label="Start a build",
        builderNames=["Builds"],

        codebases=[
            AppParameter(
                "",
                name="Override git repo",
            ),
        ],
        reason=util.StringParameter(name="reason",
                                    label="reason:",
                                    required=True, default="Manual build", size=80),
        properties=[
            util.StringParameter(name="buildname",
                                 label="App ID:",
                                 required=False),
            util.StringParameter(name="force-arches",
                                 label="Arches: (comma separated)",
                                 required=False),
            util.BooleanParameter(name="force-test-build",
                                  label="Force a test build (even for normal location)",
                                  default=False)
        ]
    )

class FlatpakBuildStep(buildbot.process.buildstep.ShellMixin, steps.BuildStep):
    def __init__(self, **kwargs):
        self.setupShellMixin({'logEnviron': False,
                              'timeout': 3600,
                              'usePTY': True})
        steps.BuildStep.__init__(self, haltOnFailure = True, **kwargs)
        self.addLogObserver('stdio', TitleObserver())
        self.title = u"Building"

    @defer.inlineCallbacks
    def run(self):
        props = self.build.properties
        id = props.getProperty("flathub_id")
        self.title = u"Building %s" % id
        self.result_title = u"Failed to build %s" % id
        self.updateSummary()

        fb_deps_args = ["--install-deps-from=flathub"]
        if props.getProperty('flathub_default_branch') in ('beta', 'test'):
            fb_deps_args.append("--install-deps-from=flathub-beta")

        if props.getProperty("flathub_custom_buildcmd", False):
            command = ['./build.sh',
                       util.Property('flathub_arch'),
                       'repo',
                       '',
                       util.Interpolate('--sandbox --delete-build-dirs --user ' + ' '.join(fb_deps_args) + ' --extra-sources-url='+ config.upstream_sources_uri +' --extra-sources=%(prop:builddir)s/../downloads '),
                       util.Property('flathub_subject')]
        else:
            command = ['flatpak-builder', '-v', '--force-clean', '--sandbox', '--delete-build-dirs',
                       '--user', fb_deps_args,
                       util.Property('extra_fb_args'),
                       '--mirror-screenshots-url=https://dl.flathub.org/repo/screenshots', '--repo', 'repo',
                       '--extra-sources-url=' + config.upstream_sources_uri,
                       util.Interpolate('--extra-sources=%(prop:builddir)s/../downloads'),
                       '--default-branch', util.Property('flathub_default_branch'),
                       '--subject', util.Property('flathub_subject'),
                       '--add-tag=upstream-maintained' if builds.is_upstream_maintained(id) else '--remove-tag=upstream-maintained',
                       'builddir', util.Interpolate('%(prop:flathub_manifest)s')]

        rendered=yield self.build.properties.render(command)
        cmd = yield self.makeRemoteShellCommand(command=rendered)
        yield self.runCommand(cmd)

        if not cmd.didFail():
            self.result_title = u"Built %s" % id

        defer.returnValue(cmd.results())

    def setTitle(self, title):
        if title.startswith("flatpak-builder: "):
            title = title[17:]
        self.title = title
        self.updateSummary()

    def getCurrentSummary(self):
        return {u'step': self.title}

    def getResultSummary(self):
        return {u'step': self.result_title}

def extract_tags_from_metadata(rc, stdout, stderr):
    tags = []
    lines = stdout.split("\n")
    if lines[0].startswith("tags="):
        for tag in lines[0][5:].strip().split(";"):
            if len(tag) > 0:
                tags.append(tag)
    return {'flathub_built_tags': tags}

def create_build_factory():
    build_factory = util.BuildFactory()
    build_factory.addSteps([
        steps.ShellSequence(
            name='Clean workdir',
            haltOnFailure=True,
            logEnviron=False,
            hideStepIf=hide_on_success,
            commands=[
                # Clean out any previous leftover mounts and files
                shellArg('for i in .flatpak-builder/rofiles/rofiles-*/; do fusermount -u $i; done || true'),
                shellArg(['find', '.', '-mindepth', '1', '-delete'])
            ]),
        steps.Git(name="checkout manifest",
                  logEnviron=False,
                  repourl=util.Property('repository'),
                  mode='full', branch='master', submodules=True),
        steps.FileDownload(name='downloading public key',
                           haltOnFailure=True,
                           hideStepIf=hide_on_success,
                           mastersrc="flathub.gpg",
                           workerdest="flathub.gpg"),
        steps.FileDownload(name='downloading merge-sources.sh',
                           haltOnFailure=True,
                           hideStepIf=hide_on_success,
                           mode=0o755,
                           mastersrc=pyutil.sibpath(__file__, "scripts/flathub-merge-sources.sh"),
                           workerdest="merge-sources.sh"),
        steps.ShellSequence(
            name='Prepare build',
            haltOnFailure=True,
            logEnviron=False,
            commands=[
                shellArg(['ostree', '--repo=repo', '--mode=archive-z2', 'init']),
                # Add flathub remote
                shellArg(['flatpak', '--user', 'remote-add', '--if-not-exists', '--gpg-import=flathub.gpg',
                          'flathub', config.upstream_repo]),
                shellArg(['flatpak', '--user', 'remote-modify', '--url='+ config.upstream_repo, 'flathub']),
                # Add flathub-beta remote
                shellArg(['flatpak', '--user', 'remote-add', '--if-not-exists', '--gpg-import=flathub.gpg',
                          'flathub-beta', config.upstream_beta_repo]),
                shellArg(['flatpak', '--user', 'remote-modify', '--url='+ config.upstream_beta_repo, 'flathub-beta']),
                # Install org.freedesktop.appstream-glib
                shellArg(['flatpak', '--user', 'install', '--or-update', '--noninteractive', 'flathub', 'org.freedesktop.appstream-glib']),
                # Install org.flatpak.flat-manager-client
                shellArg(['flatpak', '--user', 'install', '--or-update', '--noninteractive', 'flathub', 'org.flatpak.flat-manager-client']),
            ]),
        FlatpakBuildStep(name='Build'),
        steps.SetPropertyFromCommand(name='Extract built tags',
                                     command="grep -s ^tags= builddir/metadata || true",
                                     extract_fn=extract_tags_from_metadata),
        steps.ShellCommand(
            name='Check for AppStream xml',
            doStepIf=lambda step: not step.build.getProperty('flathub_config', {}).get("skip-appstream-check"),
            haltOnFailure=True,
            logEnviron=False,
            command=util.Interpolate('stat builddir/*/share/app-info/xmls/%(prop:flathub_id)s.xml.gz')),
        steps.ShellCommand(
            name='Check for right id in AppStream xml',
            doStepIf=lambda step: not step.build.getProperty('flathub_config', {}).get("skip-appstream-check"),
            haltOnFailure=True,
            logEnviron=False,
            command=util.Interpolate('zgrep "<id>%(prop:flathub_id)s\\(\\.\\w\\+\\)*\\(.desktop\\)\\?</id>" builddir/*/share/app-info/xmls/%(prop:flathub_id)s.xml.gz')),
        steps.ShellCommand(
            name='Validate AppData file',
            doStepIf=lambda step: not step.build.getProperty('flathub_config', {}).get("skip-appstream-check"),
            haltOnFailure=True,
            logEnviron=False,
            command=util.Interpolate('flatpak run org.freedesktop.appstream-glib validate builddir/*/share/appdata/%(prop:flathub_id)s.appdata.xml')),
        steps.ShellCommand(
            name='Check that the right branch was built',
            doStepIf=build_is_official,
            haltOnFailure=True,
            logEnviron=False,
            command=util.Interpolate('test ! -d repo/refs/heads/app -o -f repo/refs/heads/app/%(prop:flathub_id)s/%(prop:flathub_arch)s/%(prop:flathub_default_branch)s')),
        steps.ShellCommand(
            name='Check for 128x128 icon',
            doStepIf=should_skip_icons_check,
            haltOnFailure=True,
            logEnviron=False,
            command=util.Interpolate('zgrep "<icon type=\\"remote\\">" builddir/*/share/app-info/xmls/%(prop:flathub_id)s.xml.gz || test -f builddir/*/share/app-info/icons/flatpak/128x128/%(prop:flathub_id)s.png')),
        steps.ShellCommand(
            name='Generate deltas',
            haltOnFailure=True,
            timeout=2400,
            logEnviron=False,
            command=util.Interpolate('flatpak build-update-repo --generate-static-deltas --static-delta-ignore-ref=*.Debug  --static-delta-ignore-ref=*.Sources repo')),
        steps.ShellSequence(
            name='Upload build',
            logEnviron=False,
            haltOnFailure=True,
            timeout=3600,
            usePTY=True,
            env={"REPO_TOKEN": computeUploadToken},
            commands=[
                # Commit screenshots
                shellArg(['mkdir', '-p', 'builddir/screenshots']),
                shellArg(['ostree', 'commit', '--repo=repo', '--canonical-permissions', util.Interpolate('--branch=screenshots/%(prop:flathub_arch)s'), 'builddir/screenshots']),
                # Push to repo
                shellArg(util.FlattenList(['flatpak', 'run', 'org.flatpak.flat-manager-client', 'push', computeExtraIdArgs,
                                           util.Interpolate("%(kw:url)s/api/v1/build/%(prop:flathub_repo_id)s", url=config.repo_manager_uri),
                                           "repo"]))
            ]),
        steps.ShellCommand(name='stash downloads',
                           logEnviron=False,
                           warnOnFailure=True,
                           command=['./merge-sources.sh', util.Interpolate('%(prop:builddir)s/../downloads')]),
        steps.ShellSequence(name='clean up',
                            alwaysRun=True,
                            logEnviron=False,
                            hideStepIf=hide_on_success,
                            commands=[
                                shellArg('for i in .flatpak-builder/rofiles/rofiles-*; do fusermount -u -z $i || true; done'),
                                shellArg(['find', '.', '-mindepth', '1', '-delete']),
                            ])
    ])
    return build_factory

class BuildDependenciesSteps(steps.BuildStep):
    parms = steps.BuildStep.parms

    def __init__(self,  **kwargs):
        for p in self.__class__.parms:
            if p in kwargs:
                setattr(self, p, kwargs.pop(p))

        steps.BuildStep.__init__(self, **kwargs)

    @defer.inlineCallbacks
    def run(self):
        log = yield self.addLog('log')

        build_name = self.build.getProperty ('flathub_name', "")
        buildnr = self.build.getProperty ('flathub_buildnumber', 0)
        deps = builds.reverse_dependency_lookup(build_name)
        if deps:
            for d in deps:
                log.addStdout("Queueing build of dependency: %s\n" % (d.get_name()))
                c = d.get_change()
                change = yield self.master.data.updates.addChange(
                    author = u"flathub",
                    comments = u'Rebuild triggered by %s build %s\n' % (build_name, buildnr),
                    category = "build-dependency",
                    repository = c['repository'],
                    branch = c['branch'],
                    properties = c['properties']
                )

        log.finish()

        defer.returnValue(buildbot.process.results.SUCCESS)

class FlatpakDownloadStep(buildbot.process.buildstep.ShellMixin, steps.BuildStep):
    def __init__(self, **kwargs):
        self.setupShellMixin({'logEnviron': False,
                              'timeout': 3600,
                              'usePTY': True})
        steps.BuildStep.__init__(self, haltOnFailure = True, **kwargs)
        self.title = u"Building"

    @defer.inlineCallbacks
    def run(self):
        props = self.build.properties
        id = props.getProperty("flathub_id")
        self.title = u"Downloading sources for %s" % id
        self.result_title = u"Failed to download sources for %s" % id
        self.updateSummary()
        if props.getProperty("flathub_custom_buildcmd", False):
            # Check for flathub-download.sh and only run if it exists
            statCmd = remotecommand.RemoteCommand('stat', {'file': 'build/flathub-download.sh'})
            yield self.runCommand(statCmd)
            if statCmd.didFail():
                self.result_title = u"Skipping download due to missing flathub-download.sh"
                defer.returnValue(buildbot.process.results.SKIPPED)
                return

            command = ['./flathub-download.sh', config.upstream_sources_path]
        else:
            command = ['flatpak-builder', '--download-only', '--no-shallow-clone', '--allow-missing-runtimes',
                       '--state-dir=' + config.upstream_sources_path,
                       config.upstream_sources_path + '/.builddir', util.Interpolate('%(prop:flathub_manifest)s')]

        rendered=yield self.build.properties.render(command)
        cmd = yield self.makeRemoteShellCommand(command=rendered)
        yield self.runCommand(cmd)

        if not cmd.didFail():
            self.result_title = u"Downloaded sources for %s" % id

        defer.returnValue(cmd.results())

    def getCurrentSummary(self):
        return {u'step': self.title}

    def getResultSummary(self):
        return {u'step': self.result_title}

def create_download_sources_factory():
    download_sources_factory = util.BuildFactory()
    download_sources_factory.addSteps([
        steps.Git(name="checkout manifest",
                  logEnviron=False,
                  repourl=util.Property('repository'),
                  mode='incremental', branch='master', submodules=True),
        steps.ShellCommand(name='ensure source dir', logEnviron=False,
                           command=['mkdir', '-p', config.upstream_sources_path]),
        FlatpakDownloadStep(name='Download')
    ])
    return download_sources_factory

def create_publish_factory():
    publish_factory = util.BuildFactory()
    publish_factory.addSteps([
        SetPublishJobStep(name='Updating original build'),
        steps.ShellSequence(name='Publishing builds',
                            logEnviron=False,
                            haltOnFailure=True,
                            timeout=None,
                            usePTY=True,
                            env={"REPO_TOKEN": config.repo_manager_token},
                            commands=[
                                shellArg([*flatmgr_client, '--output=output.json', 'publish', '--wait',
                                          util.Interpolate("%(kw:url)s/api/v1/build/%(prop:flathub_repo_id)s", url=config.repo_manager_uri)])
                            ]),
        HandleUpdateRepoStep(name='Handling update repo'),
        SetRepoStateStep(2, name='Marking build as published'),
        steps.ShellSequence(name='Purging builds',
                            logEnviron=False,
                            haltOnFailure=False,
                            usePTY=True,
                            env={"REPO_TOKEN": config.repo_manager_token},
                            commands=[
                                shellArg([*flatmgr_client, 'purge',
                                          util.Interpolate("%(kw:url)s/api/v1/build/%(prop:flathub_repo_id)s", url=config.repo_manager_uri)])
                            ]),
        PurgeOldBuildsStep(name='Getting old builds'),
        BuildDependenciesSteps(name='build dependencies'),
    ])
    return publish_factory

def create_update_repo_factory():
    update_repo_factory = util.BuildFactory()
    update_repo_factory.addSteps([
        steps.ShellSequence(name='Updating repo',
                            logEnviron=False,
                            haltOnFailure=True,
                            timeout=None,
                            usePTY=True,
                            env={"REPO_TOKEN": config.repo_manager_token},
                            commands=[
                                shellArg([*flatmgr_client, 'follow-job',
                                          util.Interpolate("%(kw:url)s/api/v1/job/%(prop:flathub_update_job_id)s", url=config.repo_manager_uri)])
                            ])
    ])
    return update_repo_factory

def create_purge_factory():
    purge_factory = util.BuildFactory()
    purge_factory.addSteps([
        steps.ShellSequence(name='Purging builds',
                            logEnviron=False,
                            haltOnFailure=False,
                            usePTY=True,
                            env={"REPO_TOKEN": config.repo_manager_token},
                            commands=[
                                shellArg([*flatmgr_client, 'purge',
                                          util.Interpolate("%(kw:url)s/api/v1/build/%(prop:flathub_repo_id)s", url=config.repo_manager_uri)])
                            ]),
        SetRepoStateStep(3, name='Marking build as deleted'),
    ])
    return purge_factory

def create_periodic_purge_factory():
    periodic_purge_factory = util.BuildFactory()
    periodic_purge_factory.addSteps([
        PeriodicPurgeStep(name="Periodic build repo management")
    ])
    return periodic_purge_factory


# Map available architectures of refs possible to use as SDKs. This is needed
# to dynamically detect what architectures are available to build against.
def get_sdks(remote):
    runtimes_command = "flatpak remote-ls --user --runtime --columns=ref --arch='*' --all {}"
    runtimes_run = subprocess.Popen(runtimes_command.format(remote), shell=True, stdout=subprocess.PIPE, universal_newlines=True)
    output, _ = runtimes_run.communicate()
    runtimes = {}

    for line in output.split('\n'):
        if len(line):
            _, name, arch, branch = line.split("/")

            if name.split('.')[-1] not in ('Platform', 'Sdk'):
                continue

            if name not in runtimes:
                runtimes[name] = {}
            if branch not in runtimes[name]:
                runtimes[name][branch] = [arch]
            else:
                runtimes[name][branch].append(arch)

    return runtimes

def lookup_runtime(runtimes, name, version):
    return runtimes.get(name, {}).get(version, [])

def split_pref(pref, default_branch):
    parts = pref.split('/')
    if len(parts) == 1:
        parts.append("")
    if len(parts) == 2:
        parts.append("")
    if parts[2] == "":
        parts[2] = default_branch
    return parts

class FlathubPropertiesStep(steps.BuildStep, CompositeStepMixin, buildbot.process.buildstep.ShellMixin):
    def __init__(self, **kwargs):
        steps.BuildStep.__init__(self, **kwargs)
        self.logEnviron = False

    @defer.inlineCallbacks
    def run(self):
        props = self.build.properties

        flathub_config = {}
        flathub_config_content = yield self.getFileContentFromWorker("flathub.json")
        if flathub_config_content is not None:
            flathub_config = json.loads(flathub_config_content)

        flathub_id = props.getProperty('flathub_id')
        official_build = props.getProperty('flathub_official_build', False)
        flathub_branch = props.getProperty('flathub_branch')
        flathub_name = flathub_id
        if flathub_branch:
            flathub_name = flathub_name + "/" + flathub_branch
        git_subject = props.getProperty('git-subject')
        buildnumber = props.getProperty('buildnumber')

        if flathub_branch:
            flathub_default_branch = flathub_branch
        elif official_build:
            flathub_default_branch = u'stable'
        else:
            flathub_default_branch = u'test'

        manifest_filename = "%s.yaml" % flathub_id
        hasYaml = yield self.pathExists("build/" + manifest_filename)
        if not hasYaml:
            manifest_filename = "%s.yml" % flathub_id
            hasYml = yield self.pathExists("build/" + manifest_filename)
            if not hasYml:
                manifest_filename = "%s.json" % flathub_id

        sdk_arches = set(flathub_arches) # Default for custom builds
        if not props.getProperty("flathub_custom_buildcmd", False):
            showManifestCmd = yield self.makeRemoteShellCommand(command=['flatpak-builder', '--show-manifest', manifest_filename],
                                                                collectStdout=True)
            yield self.runCommand(showManifestCmd)
            if showManifestCmd.didFail():
                self.result_title = "Failed to load manifest file %s" % manifest_filename
                defer.returnValue(FAILURE)
                return
            manifest = json.loads(showManifestCmd.stdout)

            (sdk_name, _, sdk_version) = split_pref(manifest["sdk"], manifest["runtime-version"])

            # Get all runtimes to check for available architectures later
            sdks = get_sdks('flathub')
            if not lookup_runtime (sdks, sdk_name, sdk_version) and flathub_default_branch in ('test', 'beta'):
                # Check also in flathub-beta for test and beta builds
                sdks = get_sdks('flathub-beta')

            sdk_arches = set(lookup_runtime (sdks, sdk_name, sdk_version))
            if len(sdk_arches) == 0:
                self.descriptionDone = ["Could not find requested SDK %s//%s" % (sdk_name, sdk_version)]
                defer.returnValue(FAILURE)
                return

        # This could have been set by the change event, otherwise look in config or default
        flathub_arches_prop = props.getProperty('flathub_arches', None)
        if not flathub_arches_prop:
            # Build architectures for which there is available SDK
            arches = set(flathub_arches) & sdk_arches

            if "only-arches" in flathub_config:
                arches = arches & set(flathub_config["only-arches"])

            if "skip-arches" in flathub_config:
                arches = arches - set(flathub_config["skip-arches"])

            flathub_arches_prop = list(arches)

        for arch in flathub_arches_prop:
            if arch not in flathub_arches:
                self.descriptionDone = ["Unsupported arch: %s" % arch]
                defer.returnValue(FAILURE)
                return

        if len(flathub_arches_prop) == 0:
            self.descriptionDone = ["No compatible architectures found for build"]
            defer.returnValue(FAILURE)
            return

        # This works around the fact that we can't pass on an empty dict
        if not flathub_config:
            flathub_config["empty"] = True

        p = {
            "flathub_name": flathub_name,
            "flathub_default_branch": flathub_default_branch,
            "flathub_subject": "%s (%s)" % (git_subject, props.getProperty('got_revision')[:8]),
            "flathub_arches": flathub_arches_prop,
            "flathub_buildnumber": buildnumber,
            "flathub_manifest": manifest_filename,
            "flathub_config": flathub_config
        }

        for k, v in p.items():
            self.setProperty(k, v, self.name, runtime=True)

        defer.returnValue(SUCCESS)

class FlathubStartCommentStep(steps.BuildStep, CompositeStepMixin):
    def __init__(self, **kwargs):
        steps.BuildStep.__init__(self, **kwargs)
        self.logEnviron = False

    @defer.inlineCallbacks
    def run(self):
        build = self.build
        props = build.properties
        flathub_issue_url = props.getProperty('flathub_issue_url', None)
        if flathub_issue_url:
            builderid = yield build.getBuilderId()
            if flathub_issue_url and config.github_api_token:
                githubApiPostComment(flathub_issue_url, "Started [test build %d](%s#/builders/%d/builds/%d)" % (build.number, config.buildbot_uri, builderid, build.number))

        defer.returnValue(buildbot.process.results.SUCCESS)

class FlathubEndCommentStep(steps.BuildStep, CompositeStepMixin):
    def __init__(self, **kwargs):
        steps.BuildStep.__init__(self, **kwargs)
        self.logEnviron = False

    @defer.inlineCallbacks
    def run(self):
        build = self.build
        props = build.properties
        flathub_issue_url = props.getProperty('flathub_issue_url', None)
        if flathub_issue_url:
            builderid = yield build.getBuilderId()
            if flathub_issue_url and config.github_api_token:
                if build.results == SUCCESS:
                    flatpakref_url = props.getProperty('flathub_flatpakref_url', None)
                    comment = (
                        "Build [%d successful](%s#/builders/%d/builds/%d)\n" % (build.number, config.buildbot_uri, builderid, build.number) +
                        "To test this build, install it from the testing repository:\n" +
                        "```\n" +
                        "flatpak install --user %s\n" % flatpakref_url +
                        "```\n"
                    )
                elif build.results == CANCELLED:
                    comment = "Build [%d was cancelled](%s#/builders/%d/builds/%d)\n" % (build.number, config.buildbot_uri, builderid, build.number)
                else:
                    comment = "Build [%d failed](%s#/builders/%d/builds/%d)\n" % (build.number, config.buildbot_uri, builderid, build.number)
                githubApiPostComment(flathub_issue_url, comment)

        defer.returnValue(buildbot.process.results.SUCCESS)

def create_build_app_factory():
    build_app_factory = util.BuildFactory()
    build_app_factory.addSteps([
        steps.ShellSequence(
            name='Install/update flat-manager-client',
            haltOnFailure=True,
            logEnviron=False,
            workdir=computeMasterBaseDir,
            commands=[
                shellArg(['flatpak', '--user', 'remote-add', '--if-not-exists', '--gpg-import=flathub.gpg', 'flathub', 'https://flathub.org/repo/flathub.flatpakrepo']),
                shellArg(['flatpak', '--user', 'install', '--noninteractive', 'flathub', 'org.flatpak.flat-manager-client']),
                shellArg(['flatpak', '--user', 'update', '--noninteractive', 'org.flatpak.flat-manager-client']),
            ]
        ),
        steps.ShellCommand(name='Update build config',
                           workdir=computeMasterBaseDir,
                           command='git pull --rebase',
                           logEnviron=False,
                           hideStepIf=hide_on_success,
                           warnOnFailure=True),
        UpdateConfig(name="Reload build config",
                     hideStepIf=hide_on_success,
                     warnOnFailure=True),
        FlathubStartCommentStep(name="Send start command",
                                hideStepIf=hide_on_success),
        steps.Git(name="checkout manifest",
                  repourl=util.Property('repository'),
                  mode='incremental', branch='master', submodules=True, logEnviron=False),
        steps.SetPropertyFromCommand(name="Getting git status for subject",
                                     command="git show --format=%s -s $(git rev-list --no-merges -n 1 HEAD)",
                                     property="git-subject", logEnviron=False,
                                     hideStepIf=hide_on_success),
        FlathubPropertiesStep(name="Set flathub properties",
                              haltOnFailure=True,
                              hideStepIf=hide_on_success),
        steps.Trigger(name='Download sources',
                      haltOnFailure=True,
                      schedulerNames=['download-sources'],
                      updateSourceStamp=True,
                      waitForFinish=True,
                      set_properties=inherit_properties(inherited_properties)),
        CreateRepoBuildStep(name='Creating build on repo manager'),
        CreateUploadToken(name='Creating upload token',
                          hideStepIf=hide_on_success),
        steps.Trigger(name='Build all platforms',
                      haltOnFailure=True,
                      schedulerNames=['build-all-platforms'],
                      updateSourceStamp=True,
                      waitForFinish=True,
                      set_properties=inherit_properties(inherited_properties)),
        CheckNewerBuildStep(name='Check for newer published builds',
                            doStepIf=build_is_official),
        steps.ShellSequence(name='Commiting builds',
                            logEnviron=False,
                            haltOnFailure=True,
                            timeout=None,
                            locks=[repo_manager_lock.access('exclusive')],
                            usePTY=True,
                            env={"REPO_TOKEN": config.repo_manager_token},
                            commands=[ shellArg(computeCommitArgs) ]),
        SetRepoStateStep(1, name='Marking build as commited',
                         hideStepIf=hide_on_success),
        steps.SetProperties(name="Set flatparef url",
                            hideStepIf=hide_on_success,
                            properties= {
                                "flathub_flatpakref_url":
                                util.Interpolate("%(kw:url)s/build-repo/%(prop:flathub_repo_id)s/%(prop:flathub_id)s.flatpakref", url=config.repo_manager_dl_uri)
                            }),
        # If build failed, purge it
    steps.Trigger(name='Deleting failed build',
                  schedulerNames=['purge'],
                  waitForFinish=True,
                  doStepIf=lambda step: step.build.getProperty('flathub_repo_id', None) and do_if_failed_except_shutdown(step),
                  hideStepIf=hide_on_success_or_skipped,
                  alwaysRun=True,
                  set_properties={
                      'flathub_repo_id' : util.Property('flathub_repo_id'),
                      'flathub_orig_buildid': computeBuildId,
                  }),
        ClearUploadToken(name='Cleanup upload token',
                         hideStepIf=hide_on_success,
                         alwaysRun=True),
        FlathubEndCommentStep(name="Send end command",
                              hideStepIf=hide_on_success,
                              alwaysRun=True),
    ])
    return build_app_factory

# We use this to rate limit the untrusted builds. There ar N normal master
# workers used for trusted builds, but only one for untrusted builds, which
# means they are serialized.
def canStartMainBuild(builder, workerforbuilder, request):
    untrusted = request.properties.getProperty('flathub_untrusted', False)
    if untrusted:
        return workerforbuilder.worker.workername.endswith("Untrusted")
    else:
        return not workerforbuilder.worker.workername.endswith("Untrusted")

@util.renderer
def computeCleanupWorkers(props):
    clean_worker = props.getProperty ('cleanup-worker', None)
    if clean_worker and clean_worker in flathub_worker_names:
        worker_names = [clean_worker]
    else:
        worker_names = flathub_worker_names
    return list(map(lambda x: "cleanup-" + x, worker_names))

def create_cleanup_factory():
    cleanup_factory = util.BuildFactory()
    cleanup_factory.addSteps([
        steps.FileDownload(name='downloading cleanup.sh',
                           haltOnFailure=True,
                           hideStepIf=hide_on_success,
                           mode=0o755,
                           mastersrc=pyutil.sibpath(__file__, "scripts/flathub-cleanup.sh"),
                           workerdest="cleanup.sh"),
        steps.ShellCommand(
            name='Status',
            logEnviron=False,
            command='./cleanup.sh')
    ])
    return cleanup_factory

def create_cleanup_all_factory():
    cleanup_all_factory = util.BuildFactory()
    cleanup_all_factory.addSteps([
        steps.Trigger(name='Clean up all workers',
                      schedulerNames=['cleanup-all-workers'],
                      waitForFinish=True,
                      set_properties={
                          "cleanup-worker" : util.Property('cleanup-worker')
                      })
    ])
    return cleanup_all_factory


# Nothing else listens to these async comments, so log on error
def githubCommentDone(response):
    if not response.ok:
        log.msg("WARNING: unable to send github comment: got status %d", response_status_code)

# the 'change_source' setting tells the buildmaster how it should find out
# about source code changes.  Here we point to the buildbot clone of pyflakes.

class FlathubGithubHandler(GitHubEventHandler):

    def handle_pull_request(self, payload, event):
        issue_nr = payload['number']
        commits = payload['pull_request']['commits']
        title = payload['pull_request']['title']
        comments = payload['pull_request']['body']
        repo_full_name = payload['repository']['full_name']
        head_sha = payload['pull_request']['head']['sha']
        repo_uri = payload['repository']['html_url']
        assoc = payload["pull_request"]["author_association"]
        issue_url = payload["pull_request"]["issue_url"]

        branch = 'refs/pull/{}/head'.format(issue_nr)

        log.msg('Processing GitHub PR #{}'.format(issue_nr),
                logLevel=logging.DEBUG)

        action = payload.get('action')
        if action not in ('opened', 'reopened', 'synchronize'):
            log.msg("GitHub PR #{} {}, ignoring".format(issue_nr, action))
            return [], 'git'

        try:
            data = builds.lookup_by_git(repo_uri, branch)
        except:
            msg = str(sys.exc_info()[1])
            log.msg("WARNING: Ignoring GitHub PR request due lookup error: %s" % (msg))
            return [], 'git'

        trusted = assoc in ["COLLABORATOR", "CONTRIBUTOR", "MEMBER", "OWNER"]

        change = data.get_change(untrusted=not trusted)
        change['category'] = 'pull-request'
        change['comments'] = u'GitHub Pull Request #%d test build\n' % (issue_nr)
        change['author'] = payload['sender']['login']
        change['properties']['flathub_issue_url'] = issue_url
        return [change], 'git'

    def handle_bot_build(self, payload, event, build_id, arch):
        assoc = payload["comment"]["author_association"]
        issue_nr = payload["issue"]["number"]
        issue_url = payload["comment"]["issue_url"]

        log.msg("Detected build test request in %s PR %d (id %s)" % (payload['repository']['html_url'], issue_nr, build_id))

        trusted = assoc in ["COLLABORATOR", "CONTRIBUTOR", "MEMBER", "OWNER"]

        repo_uri = payload['repository']['html_url']
        branch = 'refs/pull/{}/head'.format(issue_nr)
        try:
            data = builds.lookup_by_git(repo_uri, branch, build_id)
        except:
            msg = str(sys.exc_info()[1])
            log.msg("WARNING: Ignoring build test request due lookup error: %s" % (msg))
            githubApiPostComment(issue_url, "Ignoring bot build request due to repo lookup error: %s." % (msg)).addCallback(githubCommentDone)
            return [], 'git'

        force_arches=None
        if arch:
            force_arches=[arch]
        change = data.get_change(force_arches=force_arches, untrusted=not trusted)
        change['category'] = 'bot-build'
        change['comments'] = u'GitHub Pull Request #%d test build\n' % (issue_nr)
        change['author'] = payload['sender']['login']
        change['properties']['flathub_issue_url'] = issue_url

        githubApiPostComment(issue_url, "Queued test build for %s." % (data.get_name())).addCallback(githubCommentDone)

        return [change], 'git'

    def handle_issue_comment(self, payload, event):
        body = payload["comment"]["body"]
        issue_url = payload["comment"]["issue_url"]
        sender = payload['sender']['login']
        is_pull_request = 'pull_request' in payload['issue']

        if sender == 'flathubbot':
            # Ignore messages from myself
            return [], 'git'

        if not is_pull_request:
            log.msg("Ignoring comment in non-pull-request")
            return [], 'git'

        bot_re = r"(?:^|\s)%s," % config.bot_name
        just_bot_re = re.compile(bot_re)
        if just_bot_re.search(body) == None:
            # No bot command, ignore
            return [], 'git'

        # bot, build?
        id_re = r"[0-9A-Za-z_\-]+\.[0-9A-Za-z_\-.]+"
        arch_re = r"[0-9A-Za-z_]+"
        bot_build_re = re.compile(r"%s build(?: (%s))?(?: on (%s))?" % (bot_re, id_re, arch_re))

        match = bot_build_re.search(body)
        if match:
            (build_id, arch) = match.groups()
            return self.handle_bot_build(payload, event, build_id, arch)

        # No known command
        githubApiPostComment(issue_url, "I'm sorry, i did not understand that command.").addCallback(githubCommentDone)
        return [], 'git'


    def handle_push(self, payload, event):
        id = payload['repository']['name']
        git_url = payload['repository']['html_url']
        ref = payload['ref']

        if not ref.startswith("refs/heads/"):
            log.msg("Ignoring refname `{}': Not a branch".format(ref))
            return [], 'git'
        branch = ref[len("refs/heads/"):]
        if payload.get('deleted'):
            log.msg("Branch `{}' deleted, ignoring".format(branch))
            return [], 'git'

        data = builds.lookup_by_git(git_url, branch)

        if not data.official:
            log.msg("Ignoring push to branch `{}': Not official".format(branch))
            return [], 'git'

        changes = []

        commits = payload['commits']
        if payload.get('created'):
            commits = [payload['head_commit']]
        for commit in commits:
            files = []
            for kind in ('added', 'modified', 'removed'):
                files.extend(commit.get(kind, []))

            when_timestamp = dateparse(commit['timestamp'])
            log.msg("New revision: {}".format(commit['id'][:8]))

            author= u'{} <{}>'.format(commit['author']['name'],
                                      commit['author']['email'])

            change = data.get_change()
            change['revision'] = commit['id']
            change['author'] = author
            change['when_timestamp'] = when_timestamp
            change['files'] = files
            change['comments'] = commit['message']
            change['revlink'] = commit['url']
            change['properties']['github_distinct'] = commit.get('distinct', True)
            change['properties']['event'] = event
            change['category'] = u'push'

            changes.append(change)

        return changes, u'git'

class MyGitLabHandler(gitlabhooks.GitLabHandler):
    def __init__(self, master, options):
        gitlabhooks.GitLabHandler.__init__(self, master, options)

    def _process_merge_request_change(self, payload, event, codebase=None):
        log.msg('Ignoring gitlab merge request')
        return []

    def _process_change(self, payload, user, repo, repo_url, event,
                        codebase=None):
        changes = []
        ref = payload['ref']
        # project name from http headers is empty for me, so get it from repository/name
        project = payload['repository']['name']

        if not ref.startswith("refs/heads/"):
            log.msg("Ignoring refname `{}': Not a branch".format(ref))
            return changes
        branch = ref[len("refs/heads/"):]
        if payload.get('deleted'):
            log.msg("Branch `{}' deleted, ignoring".format(branch))
            return changes

        data = builds.lookup_by_git(repo_url, branch)

        for commit in payload['commits']:
            if not commit.get('distinct', True):
                log.msg('Commit `%s` is a non-distinct commit, ignoring...' %
                        (commit['id'],))
                continue

            files = []
            for kind in ('added', 'modified', 'removed'):
                files.extend(commit.get(kind, []))

            when_timestamp = dateparse(commit['timestamp'])

            log.msg("New revision: %s" % commit['id'][:8])

            change = data.get_change()
            change['author'] = '%s <%s>' % (commit['author']['name'], commit['author']['email'])
            change['files'] = files
            change['comments'] = commit['message']
            change['revision'] = commit['id']
            change['when_timestamp'] = when_timestamp
            change['revlink'] = commit['url']
            change['category'] = u'push'
            change['properties']['event'] = event

            changes.append(change)

        return changes

gitlabhooks.gitlab=MyGitLabHandler

def computeConfig():
    load_config()

    c = BuildmasterConfig = {}

    c['title'] = 'Flathub'
    c['titleURL'] = 'https://flathub.org'
    c['buildbotURL'] = config.buildbot_uri
    c['buildbotNetUsageData'] = None
    c['change_source'] = []
    c['collapseRequests'] = False
    c['protocols'] = {}
    c['protocols']['pb'] = {'port': 9989}
    c['db'] = {
        'db_url' : config.db_uri,
    }

    c['logCompressionMethod'] = 'lz4'
    c['logMaxSize'] = 60*1024*1024 # (A kde sdk build was 51mb)
    c['logMaxTailSize'] = 32768

    c['caches'] = {
        'Changes': 30,
        'Builds': 50,
        'chdicts': 30,
        'BuildRequests': 20,
        'SourceStamps': 20,
        'ssdicts': 20,
        'objectids': 10,
    }

    # configure a janitor which will delete all logs older than one month,
    # and will run on sundays at 2 am
    c['configurators'] = [util.JanitorConfigurator(
        logHorizon=timedelta(weeks=4),
        hour=2,
        dayOfWeek=6
    )]

    ####### Authentication

    auth = None
    roleMatchers=[]
    adminsRole="admins"

    if config.github_auth_client != '':
        auth = util.GitHubAuth(config.github_auth_client, config.github_auth_secret,apiVersion=4,getTeamsMembership=True)
        roleMatchers.append(util.RolesFromGroups())
        adminsRole = adminsGithubGroup
    elif config.admin_password != '':
        auth = util.UserPasswordAuth({"admin": config.admin_password})
        roleMatchers.append(util.RolesFromEmails(admins=["admin"]))

    my_authz = FlathubAuthz(
        # TODO: Allow publish/delete to commiter to repo
        allowRules=[
            util.AnyControlEndpointMatcher(role=adminsRole)
        ],
        roleMatchers=roleMatchers
    )

    c['www'] = dict(port=config.buildbot_port,
                    authz=my_authz)
    if auth:
        c['www']['auth'] = auth

    ####### Workers

    # Workers from builder.json
    c['workers'] = flathub_workers

    # We only have one "builder" for building apps ("Builds"), and one
    # builder can only run at once per workers. So, to allow multiple
    # parallel builds we create a bunch of workers on the master.
    local_workers = []
    for i in range(1,config.num_master_workers+1):
        name = 'MasterWorker%d' % i
        c['workers'].append (LocalWorker(name))
        local_workers.append(name)

    # We have also have a single worker for untrusted builds so that they
    # are serialized
    untrusted_workername = 'MasterWorkerUntrusted'
    c['workers'].append (LocalWorker(untrusted_workername))
    local_workers.append(untrusted_workername)

    if len(flathub_download_sources_workers) == 0:
        master_source_workername = 'MasterSourceWorker'
        c['workers'].append (LocalWorker(master_source_workername))
        flathub_download_sources_workers.append(master_source_workername)

    ####### Schedulers

    checkin = schedulers.AnyBranchScheduler(name="checkin",
                                            treeStableTimer=10,
                                            builderNames=["Builds"])
    build = schedulers.Triggerable(name="build-all-platforms",
                                   builderNames=computeBuildArches)
    download_sources = schedulers.Triggerable(name="download-sources",
                                              builderNames=["download-sources"])
    force_build = create_build_app_force_scheduler()
    publish = schedulers.Triggerable(name="publish",
                                     builderNames=["publish"])
    update_repo = schedulers.Triggerable(name="update-repo",
                                     builderNames=["update-repo"])
    purge = schedulers.Triggerable(name="purge",
                                   builderNames=["purge"])
    periodic_purge = schedulers.Periodic(name="PeriodicPurge",
                                         builderNames=["periodic-purge"],
                                         periodicBuildTimer=1*60*60)

    force_cleanup = schedulers.ForceScheduler(
        name="force-cleanup",
        buttonName="Force cleanup",
        label="Force a worker cleanup",
        builderNames=["Cleanup Workers"],

        codebases=[util.CodebaseParameter(codebase='', hide=True)],
        reason=util.StringParameter(name="reason",
                                    label="reason:",
                                    required=True, default="force clean", size=80),
        properties=[
            util.StringParameter(name="cleanup-worker", label="Worker: (empty for all)", required=False),
        ]
    )

    cleanup = schedulers.Triggerable(name="cleanup-all-workers",
                                     builderNames=computeCleanupWorkers)

    c['schedulers'] = [checkin, build, download_sources, force_build, publish, update_repo, purge, periodic_purge, cleanup, force_cleanup]

    ####### Builders

    c['builders'] = []
    status_builders = []

    build_factory = create_build_factory()
    for arch in flathub_arches:
        extra_fb_args = ['--arch', arch]
        if arch == 'x86_64':
            extra_fb_args = extra_fb_args + ['--bundle-sources']

        for i in range(0, num_builders_per_arch()):
            builder_name = getArchBuilderName(arch, i)
            status_builders.append(builder_name)
            c['builders'].append(
                util.BuilderConfig(name=builder_name,
                                   workernames=flathub_arch_workers[arch],
                                   properties={'flathub_arch': arch, 'extra_fb_args': extra_fb_args },
                                   factory=build_factory))

    c['builders'].append(
        util.BuilderConfig(name='download-sources',
                           workernames=flathub_download_sources_workers,
                           factory=create_download_sources_factory()))
    status_builders.append('download-sources')

    c['builders'].append(
        util.BuilderConfig(name='Builds',
                           collapseRequests=True,
                           workernames=local_workers,
                           factory=create_build_app_factory(),
                           canStartBuild=canStartMainBuild))

    c['builders'].append(
        util.BuilderConfig(name='publish',
                           workernames=local_workers,
                           factory=create_publish_factory()))

    c['builders'].append(
        util.BuilderConfig(name='update-repo',
                           workernames=local_workers,
                           factory=create_update_repo_factory()))

    c['builders'].append(
        util.BuilderConfig(name='purge',
                           workernames=local_workers,
                           factory=create_purge_factory()))

    c['builders'].append(
        util.BuilderConfig(name='periodic-purge',
                           locks=[periodic_lock.access('exclusive')],
                           workernames=local_workers,
                           factory=create_periodic_purge_factory()))

    for worker in flathub_workers:
        c['builders'].append(
            util.BuilderConfig(name='cleanup-' + worker.name,
                               workername=worker.name,
                               locks=[flatpak_worker_lock.access('exclusive')],
                               factory=create_cleanup_factory()))

    c['builders'].append(
        util.BuilderConfig(name='Cleanup Workers',
                           collapseRequests=True,
                           workernames=local_workers,
                           factory=create_cleanup_all_factory()))

    ####### Services

    c['services'] = []

    if config.github_api_token != '' and not config.disable_status_updates:
        c['services'].append(reporters.GitHubStatusPush(token=config.github_api_token,
                                                        verbose=True,
                                                        context=computeStatusContext,
                                                        startDescription='Build started.',
                                                        endDescription='Build done.',
                                                        builders=status_builders))

    ####### Changes

    c['www']['change_hook_dialects'] = { }

    if config.github_change_secret != "":
        c['www']['change_hook_dialects']['github'] = {
            'class': FlathubGithubHandler,
            'secret': config.github_change_secret,
            'strict': True
        }

    if config.gitlab_change_secret and config.gitlab_change_secret != "":
        c['www']['change_hook_dialects']['gitlab'] = {
            'secret': config.gitlab_change_secret
        }

    return c
