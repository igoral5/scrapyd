import sys
import os
from datetime import datetime
from multiprocessing import cpu_count
from tempfile import NamedTemporaryFile

from twisted.internet import reactor, defer, protocol, error
from twisted.application.service import Service
from twisted.python import log

from scrapyd.utils import get_crawl_args, native_stringify_dict
from scrapyd import __version__
from .interfaces import IPoller, IEnvironment

class Launcher(Service):

    name = 'launcher'

    def __init__(self, config, app):
        self.processes = {}
        self.finished = []
        self.finished_to_keep = config.getint('finished_to_keep', 100)
        self.max_proc = self._get_max_proc(config)
        self.runner = config.get('runner', 'scrapyd.runner')
        self.app = app

    def startService(self):
        for slot in range(self.max_proc):
            self._wait_for_project(slot)
        log.msg(format='Scrapyd %(version)s started: max_proc=%(max_proc)r, runner=%(runner)r',
                version=__version__, max_proc=self.max_proc,
                runner=self.runner, system='Launcher')

    def _wait_for_project(self, slot):
        poller = self.app.getComponent(IPoller)
        poller.next().addCallback(self._spawn_process, slot)

    def _spawn_process(self, message, slot):
        msg = native_stringify_dict(message, keys_only=False)
        file_settings = msg.pop('file_settings', None)
        project = msg['_project']
        args = [sys.executable, '-m', self.runner, 'crawl']
        args += get_crawl_args(msg)
        e = self.app.getComponent(IEnvironment)
        env = e.get_environment(msg, slot)
        env = native_stringify_dict(env, keys_only=False)
        tmpfile = None
        if file_settings:
            with NamedTemporaryFile('w', encoding='utf-8', suffix='.py', delete=False) as tmp:
                tmp.write(file_settings)
            path, name_file  = os.path.split(tmp.name)
            module = os.path.splitext(name_file)[0]
            env['PYTHONPATH'] = '{}:{}'.format(path, os.environ.get('PYTHONPATH')) \
                if os.environ.get('PYTHONPATH') else path
            env['SCRAPY_SETTINGS_MODULE_TO_OVERRIDE'] = module
            tmpfile = tmp.name
        pp = ScrapyProcessProtocol(slot, project, msg['_spider'], \
            msg['_job'], env, tmpfile)
        pp.deferred.addBoth(self._process_finished, slot)
        reactor.spawnProcess(pp, sys.executable, args=args, env=env)
        self.processes[slot] = pp

    def _process_finished(self, _, slot):
        process = self.processes.pop(slot)
        process.end_time = datetime.now()
        self.finished.append(process)
        del self.finished[:-self.finished_to_keep] # keep last 100 finished jobs
        self._wait_for_project(slot)

    def _get_max_proc(self, config):
        max_proc = config.getint('max_proc', 0)
        if not max_proc:
            try:
                cpus = cpu_count()
            except NotImplementedError:
                cpus = 1
            max_proc = cpus * config.getint('max_proc_per_cpu', 4)
        return max_proc

class ScrapyProcessProtocol(protocol.ProcessProtocol):

    def __init__(self, slot, project, spider, job, env, tmpfile):
        self.slot = slot
        self.pid = None
        self.project = project
        self.spider = spider
        self.job = job
        self.start_time = datetime.now()
        self.end_time = None
        self.env = env
        self.logfile = env.get('SCRAPY_LOG_FILE')
        self.itemsfile = env.get('SCRAPY_FEED_URI')
        self.tmpfile = tmpfile
        self.deferred = defer.Deferred()

    def outReceived(self, data):
        log.msg(data.rstrip(), system="Launcher,%d/stdout" % self.pid)

    def errReceived(self, data):
        log.msg(data.rstrip(), system="Launcher,%d/stderr" % self.pid)

    def connectionMade(self):
        self.pid = self.transport.pid
        self.log("Process started: ")

    def processEnded(self, status):
        if self.tmpfile:
            os.remove(self.tmpfile)
        if isinstance(status.value, error.ProcessDone):
            self.log("Process finished: ")
        else:
            self.log("Process died: exitstatus=%r " % status.value.exitCode)
        self.deferred.callback(self)

    def log(self, action):
        fmt = '%(action)s project=%(project)r spider=%(spider)r job=%(job)r pid=%(pid)r log=%(log)r items=%(items)r'
        log.msg(format=fmt, action=action, project=self.project, spider=self.spider,
                job=self.job, pid=self.pid, log=self.logfile, items=self.itemsfile)
