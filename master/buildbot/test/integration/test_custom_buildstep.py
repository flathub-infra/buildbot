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
from twisted.internet import error

from buildbot.config import BuilderConfig
from buildbot.process import buildstep
from buildbot.process import logobserver
from buildbot.process import results
from buildbot.process.factory import BuildFactory
from buildbot.test.util.integration import RunFakeMasterTestCase


class TestLogObserver(logobserver.LogObserver):

    def __init__(self):
        self.observed = []

    def outReceived(self, txt):
        self.observed.append(txt)


class Latin1ProducingCustomBuildStep(buildstep.BuildStep):

    @defer.inlineCallbacks
    def run(self):
        _log = yield self.addLog('xx')
        output_str = '\N{CENT SIGN}'
        yield _log.addStdout(output_str)
        yield _log.finish()
        return results.SUCCESS


class BuildStepWithFailingLogObserver(buildstep.BuildStep):

    @defer.inlineCallbacks
    def run(self):
        self.addLogObserver('xx', logobserver.LineConsumerLogObserver(self.log_consumer))

        _log = yield self.addLog('xx')
        yield _log.addStdout('line1\nline2\n')
        yield _log.finish()

        return results.SUCCESS

    def log_consumer(self):
        _, _ = yield
        raise RuntimeError('fail')


class FailingCustomStep(buildstep.BuildStep):

    flunkOnFailure = True

    def __init__(self, exception=buildstep.BuildStepFailed, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.exception = exception

    @defer.inlineCallbacks
    def run(self):
        yield defer.succeed(None)
        raise self.exception()


class RunSteps(RunFakeMasterTestCase):

    @defer.inlineCallbacks
<<<<<<< HEAD
    def setUp(self):
        self.setUpTestReactor()
        self.master = fakemaster.make_master(self, wantData=True,
                                             wantMq=True, wantDb=True)
        self.master.db.insertTestData([
            fakedb.Builder(id=80, name='test'), ])

        self.builder = builder.Builder('test')
        self.builder._builderid = 80
        self.builder.config_version = 0
        self.builder.master = self.master
        self.builder.botmaster = mock.Mock()
        self.builder.botmaster.getLockFromLockAccesses = lambda l, c: []
        yield self.builder.startService()

        self.factory = factory.BuildFactory()  # will have steps added later
        new_config = config.MasterConfig()
        new_config.builders.append(
            config.BuilderConfig(name='test', workername='testworker',
                                 factory=self.factory))
        yield self.builder.reconfigServiceWithBuildbotConfig(new_config)

        self.worker = Worker('worker', 'pass')
        self.worker.sendBuilderList = lambda: defer.succeed(None)
        self.worker.parent = mock.Mock()
        self.worker.master.botmaster = mock.Mock()
        self.worker.botmaster.maybeStartBuildsForWorker = lambda w: None
        self.worker.botmaster.getBuildersForWorker = lambda w: []
        self.worker.parent = self.master
        self.worker.startService()
        self.conn = fakeprotocol.FakeConnection(self.master, self.worker)
        yield self.worker.attached(self.conn)

        wfb = self.workerforbuilder = workerforbuilder.WorkerForBuilder()
        wfb.setBuilder(self.builder)
        yield wfb.attached(self.worker, {})

        # add the buildset/request
        sourcestamps = [
            {'repository': '', 'project': '', 'codebase': ''}
        ]
        self.bsid, brids = yield self.master.db.buildsets.addBuildset(sourcestamps=sourcestamps,
                                                                      reason='x', properties={},
                                                                      builderids=[80],
                                                                      waited_for=False)

        self.brdict = \
            yield self.master.db.buildrequests.getBuildRequest(brids[80])

        self.buildrequest = \
            yield buildrequest.BuildRequest.fromBrdict(self.master, self.brdict)
=======
    def create_config_for_step(self, step):
        config_dict = {
            'builders': [
                BuilderConfig(name="builder",
                              workernames=["worker1"],
                              factory=BuildFactory([step])
                              ),
            ],
            'workers': [self.createLocalWorker('worker1')],
            'protocols': {'null': {}},
            # Disable checks about missing scheduler.
            'multiMaster': True,
        }

        yield self.setup_master(config_dict)
        builder_id = yield self.master.data.updates.findBuilderId('builder')
        return builder_id
>>>>>>> v2.9.0

    @defer.inlineCallbacks
    def test_step_raising_buildstepfailed_in_start(self):
        builder_id = yield self.create_config_for_step(FailingCustomStep())

        yield self.do_test_build(builder_id)
        yield self.assertBuildResults(1, results.FAILURE)

    @defer.inlineCallbacks
    def test_step_raising_exception_in_start(self):
        builder_id = yield self.create_config_for_step(FailingCustomStep(exception=ValueError))

        yield self.do_test_build(builder_id)
        yield self.assertBuildResults(1, results.EXCEPTION)
        self.assertEqual(len(self.flushLoggedErrors(ValueError)), 1)

    @defer.inlineCallbacks
    def test_step_raising_connectionlost_in_start(self):
        ''' Check whether we can recover from raising ConnectionLost from a step if the worker
            did not actually disconnect
        '''
        step = FailingCustomStep(exception=error.ConnectionLost)
        builder_id = yield self.create_config_for_step(step)

        yield self.do_test_build(builder_id)
        yield self.assertBuildResults(1, results.EXCEPTION)
    test_step_raising_connectionlost_in_start.skip = "Results in infinite loop"

    @defer.inlineCallbacks
    def test_step_raising_in_log_observer(self):
        step = BuildStepWithFailingLogObserver()
        builder_id = yield self.create_config_for_step(step)

        yield self.do_test_build(builder_id)
        yield self.assertBuildResults(1, results.EXCEPTION)
        yield self.assertStepStateString(2, "finished (exception)")
        self.assertEqual(len(self.flushLoggedErrors(RuntimeError)), 1)

    @defer.inlineCallbacks
    def test_Latin1ProducingCustomBuildStep(self):
        step = Latin1ProducingCustomBuildStep(logEncoding='latin-1')
        builder_id = yield self.create_config_for_step(step)

        yield self.do_test_build(builder_id)
        yield self.assertLogs(1, {
            'xx': 'o\N{CENT SIGN}\n',
        })
