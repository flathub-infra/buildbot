# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

from twisted.internet import defer

from buildbot.data import base
from buildbot.data import types
from buildbot.data.resultspec import ResultSpec
from buildbot.process.properties import Properties


class Db2DataMixin:

    def _generate_filtered_properties(self, props, filters):
        """
        This method returns Build's properties according to property filters.

        .. seealso::

            `Official Documentation <http://docs.buildbot.net/latest/developer/rtype-build.html>`_

        :param props: The Build's properties as a dict (from db)
        :param filters: Desired properties keys as a list (from API URI)

        """
        # by default none properties are returned
        if props and filters:  # pragma: no cover
            return (props
                    if '*' in filters
                    else dict(((k, v) for k, v in props.items() if k in filters)))

    def db2data(self, dbdict):
        data = {
            'buildid': dbdict['id'],
            'number': dbdict['number'],
            'builderid': dbdict['builderid'],
            'buildrequestid': dbdict['buildrequestid'],
            'workerid': dbdict['workerid'],
            'masterid': dbdict['masterid'],
            'started_at': dbdict['started_at'],
            'complete_at': dbdict['complete_at'],
            'complete': dbdict['complete_at'] is not None,
            'state_string': dbdict['state_string'],
            'results': dbdict['results'],
            'flathub_name': dbdict['flathub_name'],
            'flathub_repo_id': dbdict['flathub_repo_id'],
            'flathub_repo_status': dbdict['flathub_repo_status'],
            'flathub_build_type': dbdict['flathub_build_type'],
            'properties': {}
        }
        return defer.succeed(data)
    fieldMapping = {
        'buildid': 'builds.id',
        'number': 'builds.number',
        'builderid': 'builds.builderid',
        'buildrequestid': 'builds.buildrequestid',
        'workerid': 'builds.workerid',
        'masterid': 'builds.masterid',
        'started_at': 'builds.started_at',
        'complete_at': 'builds.complete_at',
        'state_string': 'builds.state_string',
        'results': 'builds.results',
        'flathub_name': 'builds.flathub_name',
        'flathub_repo_id': 'builds.flathub_repo_id',
        'flathub_repo_status': 'builds.flathub_repo_status',
        'flathub_build_type': 'builds.flathub_build_type',
    }


class BuildEndpoint(Db2DataMixin, base.BuildNestingMixin, base.Endpoint):

    isCollection = False
    pathPatterns = """
        /builds/n:buildid
        /builders/n:builderid/builds/n:number
        /builders/i:buildername/builds/n:number
    """

    @defer.inlineCallbacks
    def get(self, resultSpec, kwargs):
        if 'buildid' in kwargs:
            dbdict = yield self.master.db.builds.getBuild(kwargs['buildid'])
        else:
            bldr = yield self.getBuilderId(kwargs)
            if bldr is None:
                return
            num = kwargs['number']
            dbdict = yield self.master.db.builds.getBuildByNumber(bldr, num)

        data = yield self.db2data(dbdict) if dbdict else None
        # In some cases, data could be None
        if data:
            filters = resultSpec.popProperties() if hasattr(
                resultSpec, 'popProperties') else []
            # Avoid to request DB for Build's properties if not specified
            if filters:  # pragma: no cover
                try:
                    props = yield self.master.db.builds.getBuildProperties(data['buildid'])
                except (KeyError, TypeError):
                    props = {}
                filtered_properties = self._generate_filtered_properties(
                    props, filters)
                if filtered_properties:
                    data['properties'] = filtered_properties
        return data

    @defer.inlineCallbacks
    def actionStop(self, args, kwargs):
        buildid = kwargs.get('buildid')
        if buildid is None:
            bldr = kwargs['builderid']
            num = kwargs['number']
            dbdict = yield self.master.db.builds.getBuildByNumber(bldr, num)
            buildid = dbdict['id']
        self.master.mq.produce(("control", "builds",
                                str(buildid), 'stop'),
                               dict(reason=kwargs.get('reason', args.get('reason', 'no reason'))))

    @defer.inlineCallbacks
    def actionRebuild(self, args, kwargs):
        # we use the self.get and not self.data.get to be able to support all
        # the pathPatterns of this endpoint
        build = yield self.get(ResultSpec(), kwargs)
        buildrequest = yield self.master.data.get(('buildrequests', build['buildrequestid']))
        res = yield self.master.data.updates.rebuildBuildrequest(buildrequest)
        return res

    @defer.inlineCallbacks
    def actionPublish(self, args, kwargs):
        build = yield self.get(ResultSpec(), kwargs)
        repo_id=build.get('flathub_repo_id', None)
        if not repo_id:
            raise ValueError("No repo manager id for build")
        official_build=build.get('flathub_build_type', 0) == 1
        if not official_build:
            raise ValueError("Not official build, can't publish")
        sch = self.master.scheduler_manager.namedServices['publish']
        trigger_properties = Properties()
        trigger_properties.setProperty('flathub_repo_id', repo_id, 'publish', runtime=True)
        trigger_properties.setProperty('flathub_orig_buildid', build['buildid'], 'publish', runtime=True)
        trigger_properties.setProperty('flathub_name', build['flathub_name'], 'publish', runtime=True)
        trigger_properties.setProperty('flathub_buildnumber', build['number'], 'publish', runtime=True)
        idsDeferred, resultsDeferred = sch.trigger(waited_for=False,
                                                   set_props=trigger_properties,
                                                   parent_buildid=build['buildid'],
                                                   parent_relationship="Published from")
        bsid, brids = yield idsDeferred
        defer.returnValue((bsid, brids))

    @defer.inlineCallbacks
    def actionDelete(self, args, kwargs):
        build = yield self.get(ResultSpec(), kwargs)
        repo_id=build.get('flathub_repo_id', None)
        if not repo_id:
            raise ValueError("No repo manager id for build")
        sch = self.master.scheduler_manager.namedServices['purge']
        trigger_properties = Properties()
        trigger_properties.setProperty('flathub_repo_id', repo_id, 'delete', runtime=True)
        trigger_properties.setProperty('flathub_orig_buildid', build['buildid'], 'delete', runtime=True)
        name = build['flathub_name']
        if name:
            s = name.split("/",1)
            trigger_properties.setProperty('flathub_id', s[0], 'delete', runtime=True)
            if len(s) > 1:
                trigger_properties.setProperty('flathub_branch', s[1], 'delete', runtime=True)
        idsDeferred, resultsDeferred = sch.trigger(waited_for=False,
                                                   set_props=trigger_properties,
                                                   parent_buildid=build['buildid'],
                                                   parent_relationship="Deleted from")
        bsid, brids = yield idsDeferred
        defer.returnValue((bsid, brids))


class BuildsEndpoint(Db2DataMixin, base.BuildNestingMixin, base.Endpoint):

    isCollection = True
    pathPatterns = """
        /builds
        /builders/n:builderid/builds
        /builders/i:buildername/builds
        /buildrequests/n:buildrequestid/builds
        /changes/n:changeid/builds
        /workers/n:workerid/builds
    """
    rootLinkName = 'builds'

    @defer.inlineCallbacks
    def get(self, resultSpec, kwargs):
        changeid = kwargs.get('changeid')
        if changeid is not None:
            builds = yield self.master.db.builds.getBuildsForChange(changeid)
        else:
            # following returns None if no filter
            # true or false, if there is a complete filter
            builderid = None
            if 'builderid' in kwargs or 'buildername' in kwargs:
                builderid = yield self.getBuilderId(kwargs)
                if builderid is None:
                    return []
            complete = resultSpec.popBooleanFilter("complete")
            buildrequestid = resultSpec.popIntegerFilter("buildrequestid")
            resultSpec.fieldMapping = self.fieldMapping
            builds = yield self.master.db.builds.getBuilds(
                builderid=builderid,
                buildrequestid=kwargs.get('buildrequestid', buildrequestid),
                workerid=kwargs.get('workerid'),
                complete=complete,
                resultSpec=resultSpec)

        # returns properties' list
        filters = resultSpec.popProperties()

        buildscol = []
        for b in builds:
            data = yield self.db2data(b)
            # Avoid to request DB for Build's properties if not specified
            if filters:  # pragma: no cover
                props = yield self.master.db.builds.getBuildProperties(b['id'])
                filtered_properties = self._generate_filtered_properties(
                    props, filters)
                if filtered_properties:
                    data['properties'] = filtered_properties

            buildscol.append(data)
        return buildscol


class Build(base.ResourceType):

    name = "build"
    plural = "builds"
    endpoints = [BuildEndpoint, BuildsEndpoint]
    keyFields = ['builderid', 'buildid', 'workerid']
    eventPathPatterns = """
        /builders/:builderid/builds/:number
        /builds/:buildid
        /workers/:workerid/builds/:buildid
    """

    class EntityType(types.Entity):
        buildid = types.Integer()
        number = types.Integer()
        builderid = types.Integer()
        buildrequestid = types.Integer()
        workerid = types.Integer()
        masterid = types.Integer()
        started_at = types.DateTime()
        complete = types.Boolean()
        complete_at = types.NoneOk(types.DateTime())
        results = types.NoneOk(types.Integer())
        state_string = types.String()
        properties = types.NoneOk(types.SourcedProperties())
        flathub_name = types.NoneOk(types.String())
        flathub_repo_id = types.NoneOk(types.String())
        flathub_repo_status = types.NoneOk(types.Integer())
        flathub_build_type = types.NoneOk(types.Integer())
    entityType = EntityType(name)

    @defer.inlineCallbacks
    def generateEvent(self, _id, event):
        # get the build and munge the result for the notification
        build = yield self.master.data.get(('builds', str(_id)))
        self.produceEvent(build, event)

    @base.updateMethod
    @defer.inlineCallbacks
    def addBuild(self, builderid, buildrequestid, workerid,
                 flathub_name=None,
                 flathub_build_type=None):
        res = yield self.master.db.builds.addBuild(
            builderid=builderid,
            buildrequestid=buildrequestid,
            workerid=workerid,
            masterid=self.master.masterid,
            flathub_name=flathub_name,
            flathub_build_type=flathub_build_type,
            state_string='created')
        return res

    @base.updateMethod
    def generateNewBuildEvent(self, buildid):
        return self.generateEvent(buildid, "new")

    @base.updateMethod
    @defer.inlineCallbacks
    def setBuildStateString(self, buildid, state_string):
        res = yield self.master.db.builds.setBuildStateString(
            buildid=buildid, state_string=state_string)
        yield self.generateEvent(buildid, "update")
        return res

    @base.updateMethod
    @defer.inlineCallbacks
    def setBuildFlathubName(self, buildid, name):
        res = yield self.master.db.builds.setBuildFlathubName(
            buildid=buildid, name=name)
        yield self.generateEvent(buildid, "update")
        defer.returnValue(res)

    @base.updateMethod
    @defer.inlineCallbacks
    def setBuildFlathubRepoId(self, buildid, repo_id):
        res = yield self.master.db.builds.setBuildFlathubRepoId(
            buildid=buildid, repo_id=repo_id)
        yield self.generateEvent(buildid, "update")
        defer.returnValue(res)

    @base.updateMethod
    @defer.inlineCallbacks
    def setBuildFlathubRepoStatus(self, buildid, repo_status):
        res = yield self.master.db.builds.setBuildFlathubRepoStatus(
            buildid=buildid, repo_status=repo_status)
        yield self.generateEvent(buildid, "update")
        defer.returnValue(res)

    @base.updateMethod
    @defer.inlineCallbacks
    def setBuildFlathubBuildType(self, buildid, build_type):
        res = yield self.master.db.builds.setBuildFlathubBuildType(
            buildid=buildid, build_type=build_type)
        yield self.generateEvent(buildid, "update")
        defer.returnValue(res)

    @base.updateMethod
    @defer.inlineCallbacks
    def finishBuild(self, buildid, results):
        res = yield self.master.db.builds.finishBuild(
            buildid=buildid, results=results)
        yield self.generateEvent(buildid, "finished")
        return res
