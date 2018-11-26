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


import json

import sqlalchemy as sa

from twisted.internet import defer
from twisted.internet import reactor

from buildbot.db import NULL
from buildbot.db import base
from buildbot.util import epoch2datetime


class BuildsConnectorComponent(base.DBConnectorComponent):
    # Documentation is in developer/db.rst

    # returns a Deferred that returns a value
    def _getBuild(self, whereclause):
        def thd(conn):
            q = self.db.model.builds.select(whereclause=whereclause)
            res = conn.execute(q)
            row = res.fetchone()

            rv = None
            if row:
                rv = self._builddictFromRow(row)
            res.close()
            return rv
        return self.db.pool.do(thd)

    def getBuild(self, buildid):
        return self._getBuild(self.db.model.builds.c.id == buildid)

    def getBuildByNumber(self, builderid, number):
        return self._getBuild(
            (self.db.model.builds.c.builderid == builderid) &
            (self.db.model.builds.c.number == number))

    # returns a Deferred that returns a value
    def _getRecentBuilds(self, whereclause, offset=0, limit=1):
        def thd(conn):
            tbl = self.db.model.builds

            q = tbl.select(whereclause=whereclause,
                           order_by=[sa.desc(tbl.c.complete_at)],
                           offset=offset,
                           limit=limit)

            res = conn.execute(q)
            return list([self._builddictFromRow(row)
                         for row in res.fetchall()])

        return self.db.pool.do(thd)

    @defer.inlineCallbacks
    def getPrevSuccessfulBuild(self, builderid, number, ssBuild):
        gssfb = self.master.db.sourcestamps.getSourceStampsForBuild
        rv = None
        tbl = self.db.model.builds
        offset = 0
        matchssBuild = {(ss['repository'],
                         ss['branch'],
                         ss['codebase']) for ss in ssBuild}
        while rv is None:
            # Get some recent successful builds on the same builder
            prevBuilds = yield self._getRecentBuilds(whereclause=((tbl.c.builderid == builderid) &
                                                                  (tbl.c.number < number) &
                                                                  (tbl.c.results == 0)),
                                                     offset=offset,
                                                     limit=10)
            if not prevBuilds:
                break
            for prevBuild in prevBuilds:
                prevssBuild = {(ss['repository'],
                                ss['branch'],
                                ss['codebase']) for ss in (yield gssfb(prevBuild['id']))}
                if prevssBuild == matchssBuild:
                    # A successful build with the same
                    # repository/branch/codebase was found !
                    rv = prevBuild
                    break
            offset += 10

        return rv

    # returns a Deferred that returns a value
    def getBuilds(self, builderid=None, buildrequestid=None, workerid=None, complete=None, resultSpec=None):
        def thd(conn):
            tbl = self.db.model.builds
            q = tbl.select()
            if builderid is not None:
                q = q.where(tbl.c.builderid == builderid)
            if buildrequestid is not None:
                q = q.where(tbl.c.buildrequestid == buildrequestid)
            if workerid is not None:
                q = q.where(tbl.c.workerid == workerid)
            if complete is not None:
                if complete:
                    q = q.where(tbl.c.complete_at != NULL)
                else:
                    q = q.where(tbl.c.complete_at == NULL)

            if resultSpec is not None:
                return resultSpec.thd_execute(conn, q, self._builddictFromRow)

            res = conn.execute(q)
            return [self._builddictFromRow(row) for row in res.fetchall()]

        return self.db.pool.do(thd)

    # returns a Deferred that returns a value
    def addBuild(self, builderid, buildrequestid, workerid, masterid,
                 state_string, _reactor=reactor, _race_hook=None,
                 flathub_name=None,
                 flathub_build_type=None):
        started_at = _reactor.seconds()

        def thd(conn):
            tbl = self.db.model.builds
            # get the highest current number
            r = conn.execute(sa.select([sa.func.max(tbl.c.number)],
                                       whereclause=(tbl.c.builderid == builderid)))
            number = r.scalar()
            new_number = 1 if number is None else number + 1
            # insert until we are successful..
            while True:
                if _race_hook:
                    _race_hook(conn)

                try:
                    r = conn.execute(self.db.model.builds.insert(),
                                     dict(number=new_number, builderid=builderid,
                                          buildrequestid=buildrequestid,
                                          workerid=workerid, masterid=masterid,
                                          started_at=started_at, complete_at=None,
                                          flathub_name=flathub_name,
                                          flathub_build_type=flathub_build_type,
                                          state_string=state_string
                                     ))
                except (sa.exc.IntegrityError, sa.exc.ProgrammingError) as e:
                    # pg 9.5 gives this error which makes it pass some build
                    # numbers
                    if 'duplicate key value violates unique constraint "builds_pkey"' not in str(e):
                        new_number += 1
                    continue
                return r.inserted_primary_key[0], new_number
        return self.db.pool.do(thd)

    # returns a Deferred that returns None
    def setBuildStateString(self, buildid, state_string):
        def thd(conn):
            tbl = self.db.model.builds

            q = tbl.update(whereclause=(tbl.c.id == buildid))
            conn.execute(q, state_string=state_string)
        return self.db.pool.do(thd)

    def setBuildFlathubName(self, buildid, name):
        def thd(conn):
            tbl = self.db.model.builds

            q = tbl.update(whereclause=(tbl.c.id == buildid))
            conn.execute(q, flathub_name=name)
        return self.db.pool.do(thd)

    def setBuildFlathubRepoId(self, buildid, repo_id):
        def thd(conn):
            tbl = self.db.model.builds

            q = tbl.update(whereclause=(tbl.c.id == buildid))
            conn.execute(q, flathub_repo_id=repo_id)
        return self.db.pool.do(thd)

    def setBuildFlathubRepoStatus(self, buildid, repo_status):
        def thd(conn):
            tbl = self.db.model.builds

            q = tbl.update(whereclause=(tbl.c.id == buildid))
            conn.execute(q, flathub_repo_status=repo_status)
        return self.db.pool.do(thd)

    def setBuildFlathubBuildType(self, buildid, build_type):
        def thd(conn):
            tbl = self.db.model.builds

            q = tbl.update(whereclause=(tbl.c.id == buildid))
            conn.execute(q, flathub_build_type=build_type)
        return self.db.pool.do(thd)

    # returns a Deferred that returns None
    def finishBuild(self, buildid, results, _reactor=reactor):
        def thd(conn):
            tbl = self.db.model.builds
            q = tbl.update(whereclause=(tbl.c.id == buildid))
            conn.execute(q,
                         complete_at=_reactor.seconds(),
                         results=results)
        return self.db.pool.do(thd)

    # returns a Deferred that returns a value
    def getBuildProperties(self, bid):
        def thd(conn):
            bp_tbl = self.db.model.build_properties
            q = sa.select(
                [bp_tbl.c.name, bp_tbl.c.value, bp_tbl.c.source],
                whereclause=(bp_tbl.c.buildid == bid))
            props = []
            for row in conn.execute(q):
                prop = (json.loads(row.value), row.source)
                props.append((row.name, prop))
            return dict(props)
        return self.db.pool.do(thd)

    @defer.inlineCallbacks
    def setBuildProperty(self, bid, name, value, source):
        """ A kind of create_or_update, that's between one or two queries per
        call """
        def thd(conn):
            bp_tbl = self.db.model.build_properties
            self.checkLength(bp_tbl.c.name, name)
            self.checkLength(bp_tbl.c.source, source)
            whereclause = sa.and_(bp_tbl.c.buildid == bid,
                                  bp_tbl.c.name == name)
            q = sa.select(
                [bp_tbl.c.value, bp_tbl.c.source],
                whereclause=whereclause)
            prop = conn.execute(q).fetchone()
            value_js = json.dumps(value)
            if prop is None:
                conn.execute(bp_tbl.insert(),
                             dict(buildid=bid, name=name, value=value_js,
                                  source=source))
            elif (prop.value != value_js) or (prop.source != source):
                conn.execute(bp_tbl.update(whereclause=whereclause),
                             dict(value=value_js, source=source))
        yield self.db.pool.do(thd)

    def _builddictFromRow(self, row):
        def mkdt(epoch):
            if epoch:
                return epoch2datetime(epoch)

        return dict(
            id=row.id,
            number=row.number,
            builderid=row.builderid,
            buildrequestid=row.buildrequestid,
            workerid=row.workerid,
            masterid=row.masterid,
            started_at=mkdt(row.started_at),
            complete_at=mkdt(row.complete_at),
            state_string=row.state_string,
            results=row.results,
            flathub_name=row.flathub_name,
            flathub_repo_id=row.flathub_repo_id,
            flathub_repo_status=row.flathub_repo_status,
            flathub_build_type=row.flathub_build_type,
        )
