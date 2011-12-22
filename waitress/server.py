##############################################################################
#
# Copyright (c) 2001, 2002 Zope Foundation and Contributors.
# All Rights Reserved.
#
# This software is subject to the provisions of the Zope Public License,
# Version 2.1 (ZPL).  A copy of the ZPL should accompany this distribution.
# THIS SOFTWARE IS PROVIDED "AS IS" AND ANY AND ALL EXPRESS OR IMPLIED
# WARRANTIES ARE DISCLAIMED, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF TITLE, MERCHANTABILITY, AGAINST INFRINGEMENT, AND FITNESS
# FOR A PARTICULAR PURPOSE.
#
##############################################################################

import asyncore
import logging
import re
import socket
import sys

from waitress.adjustments import default_adj
from waitress.channel import HTTPServerChannel
from waitress.task import ThreadedTaskDispatcher

class WSGIHTTPServer(asyncore.dispatcher, object):
    """

    if __name__ == '__main__':

        from waitress.taskthreads import ThreadedTaskDispatcher
        td = ThreadedTaskDispatcher()
        td.setThreadCount(4)
        WSGIHTTPServer('', 8080, task_dispatcher=td)

        try:
            import asyncore
            while 1:
                asyncore.poll(5)

        except KeyboardInterrupt:
            print 'shutting down...'
            td.shutdown()
    """

    channel_class = HTTPServerChannel
    SERVER_IDENT = 'waitress.http'
    socketmod = socket # testing shim

    level_mapping = {
        'info': logging.INFO,
        'error': logging.ERROR,
        'warning': logging.WARN,
        }

    def __init__(self, application, ip, port, task_dispatcher=None,
                 adj=None, start=True, hit_log=None, verbose=False,
                 map=None, logger=None, sock=None, sub_protocol=None):
        self.application = application

        if sub_protocol:
            self.SERVER_IDENT += ' (%s)' % str(sub_protocol)

        if sys.platform[:3] == "win" and ip == 'localhost':
            ip = ''
        if adj is None:
            adj = default_adj
        self.adj = adj
        asyncore.dispatcher.__init__(self, sock, map=map)
        self.port = port
        self.task_dispatcher = task_dispatcher
        if sock is None:
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.set_reuse_addr()
        self.bind((ip, port))
        self.verbose = verbose
        self.hit_log = hit_log
        if logger is None:
            logger = logging.getLogger(self.__class__.__name__)
        self.logger = logger
        self.server_name = self.computeServerName(ip)

        if start:
            self.accept_connections()

    def log(self, message):
        """See waitress.interfaces.IDispatcherLogging"""
        # Override asyncore's default log()
        self.logger.info(message)

    def log_info(self, message, type='info'):
        """See waitress.interfaces.IDispatcherLogging"""
        self.logger.log(self.level_mapping.get(type, logging.INFO), message)

    def computeServerName(self, ip=''):
        """Given an IP, try to determine the server name."""
        if ip:
            server_name = str(ip)
        else:
            server_name = str(self.socketmod.gethostname())
        # Convert to a host name if necessary.
        is_hostname = False
        for c in server_name:
            if c != '.' and not c.isdigit():
                is_hostname = True
                break
        if not is_hostname:
            if self.verbose:
                self.log_info('Computing hostname', 'info')
            try:
                server_name = self.socketmod.gethostbyaddr(server_name)[0]
            except socket.error: # pragma: no cover
                if self.verbose:
                    self.log_info('Cannot do reverse lookup', 'info')
        return server_name

    def accept_connections(self):
        self.accepting = 1
        self.socket.listen(self.adj.backlog)  # Get around asyncore NT limit
        if self.verbose:
            self.log_info('%s started.\n\tHostname: %s\n\tPort: %d' % (
                self.SERVER_IDENT,
                self.server_name,
                self.port
                ))

    def addTask(self, task):
        """See waitress.interfaces.ITaskDispatcher"""
        td = self.task_dispatcher
        if td is not None:
            td.addTask(task)
        else:
            task.service()

    def readable(self):
        """See waitress.interfaces.IDispatcher"""
        return (self.accepting and len(self._map) < self.adj.connection_limit)

    def writable(self):
        """See waitress.interfaces.IDispatcher"""
        return False

    def handle_read(self):
        """See waitress.interfaces.IDispatcherEventHandler"""
        pass

    def handle_connect(self):
        """See waitress.interfaces.IDispatcherEventHandler"""
        pass

    def handle_accept(self):
        """See waitress.interfaces.IDispatcherEventHandler"""
        try:
            v = self.accept()
            if v is None:
                return
            conn, addr = v
        except socket.error:
            # Linux: On rare occasions we get a bogus socket back from
            # accept.  socketmodule.c:makesockaddr complains that the
            # address family is unknown.  We don't want the whole server
            # to shut down because of this.
            if self.adj.log_socket_errors:
                self.log_info ('warning: server accept() threw an exception',
                               'warning')
            return
        for (level, optname, value) in self.adj.socket_options:
            conn.setsockopt(level, optname, value)
        self.channel_class(self, conn, addr, self.adj)

    def _constructWSGIEnvironment(self, task):
        env = task.getCGIEnvironment()

        # deduce the URL scheme (http or https)
        if (env.get('HTTPS', '').lower() == "on" or
            env.get('SERVER_PORT_SECURE') == "1"):
            protocol = 'https'
        else:
            protocol = 'http'

        # the following environment variables are required by the WSGI spec
        env['wsgi.version'] = (1,0)
        env['wsgi.url_scheme'] = protocol
        env['wsgi.errors'] = sys.stderr # apps should use the logging module
        env['wsgi.multithread'] = True
        env['wsgi.multiprocess'] = True
        env['wsgi.run_once'] = False
        env['wsgi.input'] = task.request_data.getBodyStream()

        # Add some proprietary proxy information.
        # Note: Derived request parsers might not have these new attributes,
        # so fail gracefully.
        try:
            env['waitress.proxy.scheme'] = task.request_data.proxy_scheme
            env['waitress.proxy.host'] = task.request_data.proxy_netloc
        except AttributeError:
            pass
        return env

    def executeRequest(self, task):
        """Overrides HTTPServer.executeRequest()."""
        env = self._constructWSGIEnvironment(task)

        # Call the application to handle the request and write a response
        result = self.application(env, curriedStartResponse(task))

        # By iterating manually at this point, we execute task.write()
        # multiple times, allowing partial data to be sent.
        for value in result:
            task.write(value)

def fakeWrite(body):
    raise NotImplementedError(
        "the waitress HTTP Server does not support the WSGI write() function.")

def curriedStartResponse(task):
    def start_response(status, headers, exc_info=None):
        if task.wroteResponseHeader() and not exc_info:
            raise AssertionError("start_response called a second time "
                                 "without providing exc_info.")
        if exc_info:
            try:
                if task.wroteResponseHeader():
                    # higher levels will catch and handle raised exception:
                    # 1. "service" method in httptask.py
                    # 2. "service" method in severchannelbase.py
                    # 3. "handlerThread" method in taskthreads.py
                    raise exc_info[0], exc_info[1], exc_info[2]
                else:
                    # As per WSGI spec existing headers must be cleared
                    task.accumulated_headers = None
                    task.response_headers = {}
            finally:
                exc_info = None
        # Prepare the headers for output
        status, reason = re.match('([0-9]*) (.*)', status).groups()
        task.setResponseStatus(status, reason)
        task.appendResponseHeaders(['%s: %s' % i for i in headers])

        # Return the write method used to write the response data.
        return fakeWrite
    return start_response

def run_paste(wsgi_app, global_conf, name='waitress.http',
              host='127.0.0.1', port=8080, threads=4):
    port = int(port)
    threads = int(threads)
    task_dispatcher = ThreadedTaskDispatcher()
    task_dispatcher.setThreadCount(threads)
    WSGIHTTPServer(wsgi_app, name, host, port, task_dispatcher=task_dispatcher)
    asyncore.loop()