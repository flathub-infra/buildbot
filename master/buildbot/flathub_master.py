# -*- python -*-
# ex: set filetype=python:

from future.utils import iteritems
from future.utils import string_types
from buildbot.process.build import Build
from buildbot.plugins import *
from buildbot.process import logobserver
from buildbot.process import remotecommand
from buildbot.process.buildstep import FAILURE
from buildbot.process.buildstep import SUCCESS
from buildbot.process.buildstep import BuildStep
from buildbot.data import resultspec
from buildbot.util import epoch2datetime
from buildbot import locks
from twisted.python import log
from twisted.internet import defer
import buildbot
import subprocess, re, json
import os
import os.path
from buildbot.steps.worker import CompositeStepMixin
from buildbot.steps.http import getSession
from buildbot.www.hooks.github import GitHubEventHandler
from datetime import timedelta
from datetime import datetime
from dateutil.parser import parse as dateparse

import requests
import txrequests
from buildbot.flathub_builds import Builds
import sys

PY2 = sys.version_info[0] == 2
def asciiize(str):
    if PY2 and isinstance (str, unicode):
        return str.encode('ascii', 'ignore')
    return str

# This makes things work with python2
def json_to_ascii(value):
    if isinstance(value, dict):
        d = {}
        for key, dict_value in value.items():
            d[json_to_ascii(key)] = json_to_ascii(dict_value)
        return d
    elif isinstance (value, list):
        l = []
        for list_value in value:
            l.append(json_to_ascii(list_value))
        return l
    else:
        return asciiize(value)

builds = Builds('builds.json')

class FlathubConfig():
    def __init__(self):
        f = open('config.json', 'r')
        config_data = json_to_ascii(json.loads(f.read ()))

        def getConfig(config_data, name, default=""):
            return config_data.get(name, default)

        def getConfigv(config_data, name, default=[]):
            return config_data.get(name, default)

        self.repo_manager_token = getConfig(config_data, 'repo-manager-token')
        self.repo_manager_uri = getConfig(config_data, 'repo-manager-uri')
        self.buildbot_port = getConfig(config_data, 'buildbot-port', 8010)
        self.num_master_workers = getConfig(config_data, 'num-master-workers', 4)
        self.buildbot_uri = getConfig(config_data, 'buildbot-uri')
        self.upstream_repo = getConfig(config_data, 'flathub-repo')
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

config = FlathubConfig()

# Buildbot does unique builds per build + worker, so if we want a single worker
# to build multiple apps of the same arch, then we need to have multiple build
# ids for each arch, so we append a number which we calculate from the build id
num_builders_per_arch = config.num_master_workers

flathub_repoclient_path = os.getcwd() + '/scripts/repoclient'

f = open('builders.json', 'r')
worker_config = json_to_ascii(json.loads(f.read ()))

# These keeps track of subset tokens for uploads by the workers
flathub_upload_tokens = {}

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

####### Custom renderers and helper functions

@util.renderer
def computeUploadToken(props):
    build_id = props.getProperty ('flathub_repo_id')
    return flathub_upload_tokens[build_id]

@util.renderer
def computeBuildId(props):
    return props.getBuild().buildid

# This is a bit weird, but we generate the builder name from the build number to spread them around
# on the various builders, or we would only ever build one at a time, because each named builder
# is serialized
def getArchBuilderName(arch, buildnr):
    b = buildnr % num_builders_per_arch
    if b == 0:
        build_extra = ""
    else:
        build_extra = "-%s" % b

    return "build-" + arch + build_extra

@util.renderer
def computeBuildArches(props):
    buildnr = props.getProperty ('flathub_buildnumber', 0)
    return map(lambda arch: getArchBuilderName(arch, buildnr), props.getProperty("flathub_arches", []))

def hide_on_success(results, s):
    return results==buildbot.process.results.SUCCESS

def hide_on_skipped(results, s):
    return results==buildbot.process.results.SKIPPED

def hide_on_success_or_skipped(results, s):
    return results==buildbot.process.results.SUCCESS or results==buildbot.process.results.SKIPPED

# Official builds are master branch on the canonical flathub git repo
def build_is_official(step):
    return step.build.getProperty ('flathub_official_build', False)


def shellArgOptional(commands):
    return util.ShellArg(logfile='stdio', command=commands)

def shellArg(commands):
    return util.ShellArg(logfile='stdio', haltOnFailure=True, command=commands)

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

class CreateRepoBuildStep(steps.BuildStep):
    name = 'CreateRepoBuildStep'
    description = 'Requesting'
    descriptionDone = 'Requested'
    renderables = ["method", "url", "headers"]
    session = None

    def __init__(self, **kwargs):
        if txrequests is None:
            raise Exception(
                "Need to install txrequest to use this step:\n\n pip install txrequests")

        self.method = 'POST'
        self.url = config.repo_manager_uri + '/api/v1/build'
        self.headers = {
            'Authorization': util.Interpolate('Bearer %s' % (config.repo_manager_token))
        }

        steps.BuildStep.__init__(self, haltOnFailure=True, **kwargs)

    def start(self):
        d = self.doRequest()
        d.addErrback(self.failed)

    @defer.inlineCallbacks
    def doRequest(self):
        # create a new session if it doesn't exist
        self.session = getSession()

        requestkwargs = {
            'method': self.method,
            'url': self.url,
            'headers': self.headers
        }

        log = self.addLog('log')

        log.addHeader('Performing %s request to %s\n' %
                      (self.method, self.url))
        data = requestkwargs.get("data", None)
        try:
            response = yield self.session.request(**requestkwargs)
        except requests.exceptions.ConnectionError as e:
            log.addStderr(
                'An exception occurred while performing the request: %s' % e)
            self.finished(FAILURE)
            return

        if response.status_code == requests.codes.ok:
            resp = json.loads(response.text)
            log.addStdout("response: " + str(resp))
            repo_id = resp['id'];
            yield self.master.data.updates.setBuildFlathubRepoId(self.build.buildid, repo_id)
            yield self.master.data.updates.setBuildFlathubRepoStatus(self.build.buildid, 0)
            # We need this in the properties too, so we can easily read it out
            self.build.setProperty('flathub_repo_id', repo_id , 'CreateRepoBuildStep', runtime=True)

        log.finish()

        self.descriptionDone = ["Status code: %d" % response.status_code]
        if (response.status_code < 400):
            self.finished(SUCCESS)
        else:
            self.finished(FAILURE)

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


class CreateUploadToken(steps.POST):
    def __init__(self, **kwargs):
        steps.POST.__init__(self, config.repo_manager_uri + '/api/v1/token_subset',
                            haltOnFailure=True,
                            headers= {
                                'Authorization': util.Interpolate('Bearer %s' % (config.repo_manager_token))
                            },
                            **kwargs)

    def start(self):
        props = self.build.properties
        prefix = [props.getProperty("flathub_id")]
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
        return steps.POST.start(self)

    def log_response(self, response):
        log = self.getLog('log')
        if response.status_code == requests.codes.ok:
            resp = json.loads(response.text)
            build_id = self.build.getProperty ('flathub_repo_id')
            flathub_upload_tokens[build_id] = resp['token']

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
                                    env={"REPO_TOKEN": config.repo_manager_token},
                                    commands=[
                                        shellArg(['echo', flathub_repoclient_path, 'purge',
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
        for b in builds:
            age=epoch2datetime(self.master.reactor.seconds()) - b['complete_at']
            if b['flathub_build_type'] == 0:
                # Test build
                if age > max_test_age:
                    self.build.addStepsAfterCurrentStep([
                        steps.Trigger(name='Deleting old test build',
                                      schedulerNames=['purge'],
                                      waitForFinish=False,
                                      set_properties={
                                          'flathub_repo_id' : b['flathub_repo_id'],
                                          'flathub_orig_buildid': b['buildid'],
                                      }),
                    ])
            if b['flathub_build_type'] == 1:
                # Official build
                pass # TODO: Handle
        self.finished(SUCCESS)

###### Init

c = BuildmasterConfig = {}
c['change_source'] = []
c['protocols'] = {}

####### Authentication

auth = None
roleMatchers=[]
adminsRole="admins"

if config.admin_password != '':
    auth = util.UserPasswordAuth({"admin": config.admin_password})
    roleMatchers.append(util.RolesFromEmails(admins=["admin"]))

if config.github_auth_client != '':
    auth = util.GitHubAuth(config.github_auth_client, config.github_auth_secret)
    roleMatchers.append(util.RolesFromGroups())
    adminsRole = 'flathub'

authz = util.Authz(
    # TODO: Allow publish/delete to commiter to repo
    allowRules=[
        util.AnyControlEndpointMatcher(role=adminsRole)
    ],
    roleMatchers=roleMatchers
)

c['protocols']['pb'] = {'port': 9989}

####### SETUP

flathub_arches = []
flathub_arch_workers = {}
flathub_download_sources_workers = []


####### WORKERS

# The 'workers' list defines the set of recognized workers. Each element is
# a Worker object, specifying a unique worker name and password.  The same
# worker name and password must be configured on the worker.
c['workers'] = []

# For whatever reason, max-builds doesn't seem to work, so we only ever run one build.
# To hack around this we create multiple master workers
local_workers = []
for i in range(1,config.num_master_workers+1):
    name = 'MasterWorker%d' % i
    c['workers'].append (worker.LocalWorker(name))
    local_workers.append(name)

build_workers = []

for w in worker_config.iterkeys():
    wc = worker_config[w]
    passwd = wc['password']
    max_builds = 1
    if wc.has_key('max-builds'):
        max_builds = wc['max-builds']
    wk = worker.Worker(w, passwd, max_builds=max_builds)
    wk.flathub_classes = []
    if wc.has_key('classes'):
        wk.flathub_classes = wc['classes']
    c['workers'].append (wk)
    if wc.has_key('arches'):
        build_workers.append(w)
        for a in wc['arches']:
            if not a in flathub_arches:
                flathub_arches.append(a)
                flathub_arch_workers[a] = []
            flathub_arch_workers[a].append(w)
    if wc.has_key('download-sources'):
        flathub_download_sources_workers.append(w)

####### SCHEDULERS
checkin = schedulers.AnyBranchScheduler(name="checkin",
                                        treeStableTimer=10,
                                        builderNames=["Builds"])

build = schedulers.Triggerable(name="build-all-platforms",
                               builderNames=computeBuildArches)
download_sources = schedulers.Triggerable(name="download-sources",
                                     builderNames=["download-sources"])
publish = schedulers.Triggerable(name="publish",
                                     builderNames=["publish"])
purge = schedulers.Triggerable(name="purge",
                                     builderNames=["purge"])
periodic_purge = schedulers.Periodic(name="PeriodicPurge",
                                     builderNames=["periodic-purge"],
                                     periodicBuildTimer=12*60*60,
)

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
        if repo_uri:
            if '/' in buildname:
                raise Exception("Can't specify non-empty version and custom git repo at the same time.")
            # Here, buildname is now always just an id
            data = builds.lookup_by_git(repo_uri, branch if branch else u'master', buildname)
            # If we specified a custom git uri then the git module name might not be the
            # app id, so in this case we need to remember the actual app id that
            # the user specified
        elif buildname:
            data = builds.lookup_by_name(buildname)
            if branch:
                data.git_branch = branch
                # We're overriding the branch, so we loose officialness
                data.official = False
        else:
            raise Exception("Must specify either repo uri or buildname")

        change = data.get_change(force_test=force_test, force_arches=force_arches)
        properties.update(change['properties'])
        return {
            'repository': change['repository'],
            'branch': change['branch'],
            'revision': '',
        }

force = schedulers.ForceScheduler(
    name="build-app",
    buttonName="Start build",
    label="Start a build",
    builderNames=["Builds"],

    codebases=[
        AppParameter(
            "",
            name="Main repository",
        ),
    ],
    reason=util.StringParameter(name="reason",
                                label="reason:",
                                required=True, default="force build", size=80),
    properties=[
        util.StringParameter(name="buildname",
                             label="Buildname:",
                             required=False),
        util.StringParameter(name="force-arches",
                             label="Arches: (comma separated)",
                             required=False),
        util.BooleanParameter(name="force-test-build",
                              label="Force a test build (even for normal location)",
                              default=False)
    ]
)

c['schedulers'] = [checkin, build, publish, purge, periodic_purge, download_sources, force]
c['collapseRequests'] = False

####### BUILDERS

flatpak_update_lock = util.WorkerLock("flatpak-update")
# This lock is taken in a shared mode for builds, but in exclusive mode when doing
# global worker operations like cleanup
flatpak_worker_lock = util.WorkerLock("flatpak-worker-lock", maxCount=1000)

# Certain repo manager jobs are serialized anyway (commit, publish), so we
# only work on one at a time
repo_manager_lock = util.MasterLock("repo-manager")

# The 'builders' list defines the Builders, which tell Buildbot how to perform a build:
# what steps, and which workers can execute them.  Note that any particular build will
# only take place on one worker.


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

        command = ['flatpak-builder', '-v', '--force-clean', '--sandbox', '--delete-build-dirs',
                   '--user', '--install-deps-from=flathub',
                   util.Property('extra_fb_args'),
                   '--mirror-screenshots-url=https://flathub.org/repo/screenshots', '--repo', 'repo',
                   '--extra-sources-url=' + config.upstream_sources_uri,
                   util.Interpolate('--extra-sources=%(prop:builddir)s/../downloads'),
                   '--default-branch', util.Property('flathub_default_branch'),
                   '--subject', util.Property('flathub_subject'),
                   'builddir', util.Interpolate('%(prop:flathub_manifest)s')]
        if props.getProperty("flathub_custom_buildcmd", False):
            command = ['./build.sh', util.Property('flathub_arch'), 'repo', '', util.Interpolate('--sandbox --delete-build-dirs --user --install-deps-from=flathub --extra-sources-url='+ config.upstream_sources_uri +' --extra-sources=%(prop:builddir)s/../downloads '), util.Property('flathub_subject')]

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
        steps.FileDownload(name='downloading repoclient',
                           haltOnFailure=True,
                           hideStepIf=hide_on_success,
                           mode=0755,
                           mastersrc="scripts/repoclient",
                           workerdest="repoclient"),
        steps.FileDownload(name='downloading merge-sources.sh',
                           haltOnFailure=True,
                           hideStepIf=hide_on_success,
                           mode=0755,
                           mastersrc="scripts/merge-sources.sh",
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
                # Update flathub remote url
                shellArg(['flatpak', '--user', 'remote-modify', '--url='+ config.upstream_repo, 'flathub']),
            ]),
        FlatpakBuildStep(name='Build'),
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
            name='Check that the right branch was built',
            haltOnFailure=True,
            logEnviron=False,
            command=util.Interpolate('test ! -d repo/refs/heads/app -o -f repo/refs/heads/app/%(prop:flathub_id)s/%(prop:flathub_arch)s/%(prop:flathub_default_branch)s')),
        steps.ShellSequence(
            name='Upload build',
            logEnviron=False,
            haltOnFailure=True,
            env={"REPO_TOKEN": computeUploadToken},
            commands=[
                # Commit screenshots
                shellArg(['mkdir', '-p', 'builddir/screenshots']),
                shellArg(['ostree', 'commit', '--repo=repo', util.Interpolate('--branch=screenshots/%(prop:flathub_arch)s'), 'builddir/screenshots']),
                # Push to repo
                shellArg(['./repoclient', 'push',
                          util.Interpolate("%(kw:url)s/api/v1/build/%(prop:flathub_repo_id)s", url=config.repo_manager_uri),
                          "repo"])
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
        official = self.build.getProperty ('flathub_official_build', False)
        if not official:
            defer.returnValue(buildbot.process.results.SUCCESS)
        build_name = self.build.getProperty ('flathub_name', "")
        buildnr = self.build.getProperty ('flathub_buildnumber', 0)
        deps = builds.reverse_dependency_lookup(build_name)
        if deps:
            for d in deps:
                c = d.get_change()
                change = yield self.master.data.updates.addChange(
                    author = u"flathub",
                    comments = u'Rebuild triggered by %s build %s\n' % (build_name, buildnr),
                    category = "build-dependency",
                    repository = c['repository'],
                    branch = c['branch'],
                    properties = c['properties']
                )

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

publish_factory = util.BuildFactory()
publish_factory.addSteps([
    steps.ShellSequence(name='Publishing builds',
                        logEnviron=False,
                        haltOnFailure=True,
                        env={"REPO_TOKEN": config.repo_manager_token},
                        commands=[
                            shellArg([flathub_repoclient_path, 'publish', '--wait',
                                      util.Interpolate("%(kw:url)s/api/v1/build/%(prop:flathub_repo_id)s", url=config.repo_manager_uri)])
                        ]),
    SetRepoStateStep(2, name='Marking build as published'),
    steps.ShellSequence(name='Purging builds',
                        logEnviron=False,
                        haltOnFailure=False,
                        env={"REPO_TOKEN": config.repo_manager_token},
                        commands=[
                            shellArg([flathub_repoclient_path, 'purge',
                                      util.Interpolate("%(kw:url)s/api/v1/build/%(prop:flathub_repo_id)s", url=config.repo_manager_uri)])
                        ]),
    PurgeOldBuildsStep(name='Getting old builds'),
])

purge_factory = util.BuildFactory()
purge_factory.addSteps([
    steps.ShellSequence(name='Purging builds',
                        logEnviron=False,
                        haltOnFailure=False,
                        env={"REPO_TOKEN": config.repo_manager_token},
                        commands=[
                            shellArg([flathub_repoclient_path, 'purge',
                                      util.Interpolate("%(kw:url)s/api/v1/build/%(prop:flathub_repo_id)s", url=config.repo_manager_uri)])
                        ]),
    SetRepoStateStep(3, name='Marking build as deleted'),
])

periodic_purge_factory = util.BuildFactory()
periodic_purge_factory.addSteps([
    PeriodicPurgeStep(name="Periodic build repo management")
])

class FlathubPropertiesStep(steps.BuildStep, CompositeStepMixin):
    def __init__(self, **kwargs):
        steps.BuildStep.__init__(self, **kwargs)
        self.logEnviron = False

    @defer.inlineCallbacks
    def run(self):
        props = self.build.properties

        flathub_config = {}
        content = yield self.getFileContentFromWorker("flathub.json")
        if content != None:
            flathub_config = json.loads(content)

        flathub_id = props.getProperty('flathub_id')
        official_build = props.getProperty('flathub_official_build', False)
        flathub_branch = props.getProperty('flathub_branch')
        flathub_name = flathub_id
        if flathub_branch:
            flathub_name = flathub_name + "/" + flathub_branch
        git_subject = props.getProperty('git-subject')
        buildnumber = props.getProperty('buildnumber')
        git_repository = props.getProperty('repository')
        git_branch = props.getProperty('branch')

        # This could have been set by the change event, otherwise look in config or default
        flathub_arches_prop = props.getProperty('flathub_arches', None)
        if not flathub_arches_prop:
            arches = set(flathub_arches)
            if flathub_config.has_key("only-arches"):
                arches = arches & set(flathub_config["only-arches"])

            if flathub_config.has_key("skip-arches"):
                arches = arches - set(flathub_config["skip-arches"])
            flathub_arches_prop = list(arches)

        # This works around the fact that we can't pass on an empty dict
        if not flathub_config:
            flathub_config["empty"] = True

        if content != None:
            flathub_config = json.loads(content)

        manifest = "%s.yaml" % flathub_id
        hasYaml = yield self.pathExists("build/" + manifest)
        if not hasYaml:
            manifest = "%s.yml" % flathub_id
            hasYml = yield self.pathExists("build/" + manifest)
            if not hasYml:
                manifest = "%s.json" % flathub_id

        p = {
            "flathub_name": flathub_name,
            "flathub_default_branch": flathub_branch if flathub_branch else u'stable',
            "flathub_subject": "%s (%s)" % (git_subject, props.getProperty('got_revision')[:8]),
            "flathub_arches": flathub_arches_prop,
            "flathub_buildnumber": buildnumber,
            "flathub_manifest": manifest,
            "flathub_config": flathub_config
        }

        for k, v in iteritems(p):
            self.setProperty(k, v, self.name, runtime=True)

        #yield self.master.data.updates.setBuildFlathubName(self.build.buildid, flathub_name)
        #yield self.master.data.updates.setBuildFlathubBuildType(self.build.buildid, 1 if official_build else 0)

        defer.returnValue(buildbot.process.results.SUCCESS)

build_app_factory = util.BuildFactory()
build_app_factory.addSteps([
    steps.Git(name="checkout manifest",
              repourl=util.Property('repository'),
              mode='incremental', branch='master', submodules=False, logEnviron=False),
    steps.SetPropertyFromCommand(name="Getting git status for subject",
                                 command="git show --format=%s -s $(git rev-list --no-merges -n 1 HEAD)",
                                 property="git-subject", logEnviron=False,
                                 hideStepIf=hide_on_success),
    FlathubPropertiesStep(name="Set flathub properties",
                          hideStepIf=hide_on_success),
    CreateRepoBuildStep(name='Creating build on repo manager'),
    CreateUploadToken(name='Creating upload token',
                      hideStepIf=hide_on_success),
    steps.Trigger(name='Download sources',
                  haltOnFailure=True,
                  schedulerNames=['download-sources'],
                  updateSourceStamp=True,
                  waitForFinish=True,
                  set_properties=inherit_properties(inherited_properties)),
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
                        locks=[repo_manager_lock.access('exclusive')],
                        env={"REPO_TOKEN": config.repo_manager_token},
                        commands=[
                            shellArg([flathub_repoclient_path, 'commit', '--wait',
                                      util.Interpolate("%(kw:url)s/api/v1/build/%(prop:flathub_repo_id)s", url=config.repo_manager_uri)])
                        ]),
    SetRepoStateStep(1, name='Marking build as commited',
                     hideStepIf=hide_on_success),
    steps.SetProperties(name="Set flatparef url",
                        hideStepIf=hide_on_success,
                        properties= {
                            "flathub_flatpakref_url":
                            util.Interpolate("%(kw:url)s/build-repo/%(prop:flathub_repo_id)s/%(prop:flathub_id)s.flatpakref", url=config.repo_manager_uri)
                        }),
    BuildDependenciesSteps(name='build dependencies'),
    # If build failed, purge it
    steps.Trigger(name='Deleting failed build',
                  schedulerNames=['purge'],
                  waitForFinish=True,
                  doStepIf=lambda step: not step.build.results == SUCCESS,
                  hideStepIf=hide_on_success_or_skipped,
                  alwaysRun=True,
                  set_properties={
                      'flathub_repo_id' : util.Property('flathub_repo_id'),
                      'flathub_orig_buildid': computeBuildId,
                  }),
    ClearUploadToken(name='Cleanup upload token',
                     hideStepIf=hide_on_success,
                     alwaysRun=True),
])

c['builders'] = []

status_builders = []

build_factory = create_build_factory()

for arch in flathub_arches:
    extra_fb_args = ['--arch', arch]
    if arch == 'x86_64':
        extra_fb_args = extra_fb_args + ['--bundle-sources']

    for i in range(0, num_builders_per_arch):
        c['builders'].append(
            util.BuilderConfig(name=getArchBuilderName(arch, i),
                               workernames=flathub_arch_workers[arch],
                               properties={'flathub_arch': arch, 'extra_fb_args': extra_fb_args },
                               factory=build_factory))
    status_builders.append('build-' + arch)
c['builders'].append(
    util.BuilderConfig(name='download-sources',
                       workernames=flathub_download_sources_workers,
                       factory=download_sources_factory))
status_builders.append('download-sources')
c['builders'].append(
    util.BuilderConfig(name='publish',
                       workernames=local_workers,
                       factory=publish_factory))
c['builders'].append(
    util.BuilderConfig(name='purge',
                       workernames=local_workers,
                       factory=purge_factory))
c['builders'].append(
    util.BuilderConfig(name='periodic-purge',
                       workernames=local_workers,
                       factory=periodic_purge_factory))
status_builders.append('download-sources')
c['builders'].append(
    util.BuilderConfig(name='Builds',
                       collapseRequests=True,
                       workernames=local_workers,
                       factory=build_app_factory))

##########################
# Builder cleanup support
##########################

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

@util.renderer
def computeCleanupWorkers(props):
    clean_worker = props.getProperty ('cleanup-worker', None)
    if clean_worker and clean_worker in build_workers:
        workers = [clean_worker]
    else:
        workers = build_workers

    return map(lambda x: "cleanup-" + x, workers)

cleanup = schedulers.Triggerable(name="cleanup-all-workers",
                                 builderNames=computeCleanupWorkers)

c['schedulers'].append(cleanup)
c['schedulers'].append(force_cleanup)

cleanup_factory = util.BuildFactory()
cleanup_factory.addSteps([
    steps.FileDownload(name='downloading cleanup.sh',
                       haltOnFailure=True,
                       hideStepIf=hide_on_success,
                       mode=0755,
                       mastersrc="scripts/cleanup.sh",
                       workerdest="cleanup.sh"),
    steps.ShellCommand(
        name='Status',
        logEnviron=False,
        command='./cleanup.sh')
    ])

cleanup_all_factory = util.BuildFactory()
cleanup_all_factory.addSteps([
    steps.Trigger(name='Clean up all workers',
                  schedulerNames=['cleanup-all-workers'],
                  waitForFinish=True,
                  set_properties={
                      "cleanup-worker" : util.Property('cleanup-worker')
                  })
])

for worker in build_workers:
    c['builders'].append(
        util.BuilderConfig(name='cleanup-' + worker,
                           workername=worker,
                           locks=[flatpak_worker_lock.access('exclusive')],
                           factory=cleanup_factory))
    status_builders.append('build-' + arch)

c['builders'].append(
    util.BuilderConfig(name='Cleanup Workers',
                       collapseRequests=True,
                       workernames=local_workers,
                       factory=cleanup_all_factory))

####### BUILDBOT SERVICES

# 'services' is a list of BuildbotService items like reporter targets. The
# status of each build will be pushed to these targets. buildbot/reporters/*.py
# has a variety to choose from, like IRC bots.

c['services'] = []

if config.github_api_token != '':
    c['services'].append(reporters.GitHubStatusPush(token=config.github_api_token,
                                                    verbose=True,
                                                    context=util.Interpolate("buildbot/%(prop:buildername)s"),
                                                    startDescription='Build started.',
                                                    endDescription='Build done.',
                                                    builders=status_builders))

####### PROJECT IDENTITY

# the 'title' string will appear at the top of this buildbot installation's
# home pages (linked to the 'titleURL').

c['title'] = 'Flathub'
c['titleURL'] = 'https://flathub.org'

# the 'buildbotURL' string should point to the location where the buildbot's
# internal web server is visible. This typically uses the port number set in
# the 'www' entry below, but with an externally-visible host name which the
# buildbot cannot figure out without some help.

c['buildbotURL'] = config.buildbot_uri

c['www'] = dict(port=config.buildbot_port,
                authz=authz)
if auth:
    c['www']['auth'] = auth

####### CHANGESOURCES

# the 'change_source' setting tells the buildmaster how it should find out
# about source code changes.  Here we point to the buildbot clone of pyflakes.

class FlathubGithubHandler(GitHubEventHandler):

    def handle_issue_comment(self, payload, event):
        body = payload["comment"]["body"]
        assoc = payload["comment"]["author_association"]
        issue_nr = payload["issue"]["number"]

        offset = body.find("bot, build");
        if offset == -1:
            return [], 'git'

        build_id = None
        rest = body[offset+len("bot, build"):]
        lines = rest.splitlines()
        if len(lines) > 0:
            words = lines[0].split()
            if len(words) > 0:
                build_id = words[0]

        log.msg("Detected build test request in %s PR %d (id %s)" % (payload['repository']['html_url'], issue_nr, build_id))

        if not assoc in ["COLLABORATOR", "CONTRIBUTOR", "MEMBER", "OWNER"]:
            log.msg("WARNING: Ignoring build test request due to lack of perms")
            return [], 'git'

        repo_uri = payload['repository']['html_url']
        branch = 'refs/pull/{}/head'.format(issue_nr),
        try:
            data = builds.lookup_by_git(repo_uri, branch)
        except:
            log.msg("WARNING: Ignoring build test request due lookup error")
            return [], 'git'

        change = d.get_change()
        change['category'] = 'bot-build'
        change['comments'] = u'GitHub Pull Request #%d test build\n' % (issue_nr)
        change['author'] = payload['sender']['login']
        return [change], 'git'

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

####### DB URL

c['db'] = {
    # This specifies what database buildbot uses to store its state.  You can leave
    # this at its default for all but the largest installations.
    'db_url' : config.db_uri,
}

# configure a janitor which will delete all logs older than one month,
# and will run on sundays at noon
c['configurators'] = [util.JanitorConfigurator(
    logHorizon=timedelta(weeks=4),
    hour=12,
    dayOfWeek=6
)]
