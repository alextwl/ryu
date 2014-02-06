# Copyright (C) 2011, 2012 Nippon Telegraph and Telephone Corporation.
# Copyright (C) 2011 Isaku Yamahata <yamahata at valinux co jp>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import inspect
import itertools
import logging
import sys

from ryu import utils
from ryu.controller.handler import register_instance, get_dependent_services
from ryu.controller.controller import Datapath
from ryu.controller import event
from ryu.controller.event import EventRequestBase, EventReplyBase
from ryu.lib import hub
from ryu.ofproto import ofproto_protocol

LOG = logging.getLogger('ryu.base.app_manager')

SERVICE_BRICKS = {}


def lookup_service_brick(name):
    return SERVICE_BRICKS.get(name)


def register_app(app):
    assert isinstance(app, RyuApp)
    assert not app.name in SERVICE_BRICKS
    SERVICE_BRICKS[app.name] = app
    register_instance(app)


def unregister_app(app):
    SERVICE_BRICKS.pop(app.name)


class RyuApp(object):
    """
    The base class for Ryu applications.

    RyuApp subclasses are instantiated after ryu-manager loaded
    all requested Ryu application modules.
    __init__ should call RyuApp.__init__ with the same arguments.
    It's illegal to send any events in __init__.

    The instance attribute 'name' is the name of the class used for
    message routing among Ryu applications.  (Cf. send_event)
    It's set to __class__.__name__ by RyuApp.__init__.
    It's discouraged for subclasses to override this.
    """

    _CONTEXTS = {}
    """
    A dictionary to specify contexts which this Ryu application wants to use.
    Its key is a name of context and its value is an ordinary class
    which implements the context.  The class is instantiated by app_manager
    and the instance is shared among RyuApp subclasses which has _CONTEXTS
    member with the same key.  A RyuApp subclass can obtain a reference to
    the instance via its __init__'s kwargs as the following.

    Example::

        _CONTEXTS = {
            'network': network.Network
        }

        def __init__(self, *args, *kwargs):
            self.network = kwargs['network']
    """

    _EVENTS = []
    """
    A list of event classes which this RyuApp subclass would generate.
    This should be specified if and only if event classes are defined in
    a different python module from the RyuApp subclass is.
    """

    OFP_VERSIONS = None
    """
    A list of supported OpenFlow versions for this RyuApp.
    The default is all versions supported by the framework.

    Examples::

        OFP_VERSIONS = [ofproto_v1_0.OFP_VERSION,
                        ofproto_v1_2.OFP_VERSION]

    If multiple Ryu applications are loaded in the system,
    the intersection of their OFP_VERSIONS is used.
    """

    @classmethod
    def context_iteritems(cls):
        """
        Return iterator over the (key, contxt class) of application context
        """
        return cls._CONTEXTS.iteritems()

    def __init__(self, *_args, **_kwargs):
        super(RyuApp, self).__init__()
        self.name = self.__class__.__name__
        self.event_handlers = {}        # ev_cls -> handlers:list
        self.observers = {}     # ev_cls -> observer-name -> states:set
        self.threads = []
        self.events = hub.Queue(128)
        if hasattr(self.__class__, 'LOGGER_NAME'):
            self.logger = logging.getLogger(self.__class__.LOGGER_NAME)
        else:
            self.logger = logging.getLogger(self.name)

        # prevent accidental creation of instances of this class outside RyuApp
        class _EventThreadStop(event.EventBase):
            pass
        self._event_stop = _EventThreadStop()
        self.is_active = True

    def start(self):
        """
        Hook that is called after startup initialization is done.
        """
        self.threads.append(hub.spawn(self._event_loop))

    def stop(self):
        self.is_active = False
        self._send_event(self._event_stop, None)
        hub.joinall(self.threads)

    def register_handler(self, ev_cls, handler):
        assert callable(handler)
        self.event_handlers.setdefault(ev_cls, [])
        self.event_handlers[ev_cls].append(handler)

    def register_observer(self, ev_cls, name, states=None):
        states = states or set()
        ev_cls_observers = self.observers.setdefault(ev_cls, {})
        ev_cls_observers.setdefault(name, set()).update(states)

    def unregister_observer(self, ev_cls, name):
        observers = self.observers.get(ev_cls, {})
        observers.pop(name)

    def unregister_observer_all_event(self, name):
        for observers in self.observers.values():
            observers.pop(name, None)

    def get_handlers(self, ev, state=None):
        handlers = self.event_handlers.get(ev.__class__, [])
        if state is None:
            return handlers

        return [handler for handler in handlers
                if not handler.dispatchers or state in handler.dispatchers]

    def get_observers(self, ev, state):
        observers = []
        for k, v in self.observers.get(ev.__class__, {}).iteritems():
            if not state or not v or state in v:
                observers.append(k)

        return observers

    def send_request(self, req):
        """
        Make a synchronous request.
        Set req.sync to True, send it to a Ryu application specified by
        req.dst, and block until receiving a reply.
        Returns the received reply.
        The argument should be an instance of EventRequestBase.
        """

        assert isinstance(req, EventRequestBase)
        req.sync = True
        req.reply_q = hub.Queue()
        self.send_event(req.dst, req)
        # going to sleep for the reply
        return req.reply_q.get()

    def _event_loop(self):
        while self.is_active or not self.events.empty():
            ev, state = self.events.get()
            if ev == self._event_stop:
                continue
            handlers = self.get_handlers(ev, state)
            for handler in handlers:
                handler(ev)

    def _send_event(self, ev, state):
        self.events.put((ev, state))

    def send_event(self, name, ev, state=None):
        """
        Send the specified event to the RyuApp instance specified by name.
        """

        if name in SERVICE_BRICKS:
            if isinstance(ev, EventRequestBase):
                ev.src = self.name
            LOG.debug("EVENT %s->%s %s" %
                      (self.name, name, ev.__class__.__name__))
            SERVICE_BRICKS[name]._send_event(ev, state)
        else:
            LOG.debug("EVENT LOST %s->%s %s" %
                      (self.name, name, ev.__class__.__name__))

    def send_event_to_observers(self, ev, state=None):
        """
        Send the specified event to all observers of this RyuApp.
        """

        for observer in self.get_observers(ev, state):
            self.send_event(observer, ev, state)

    def reply_to_request(self, req, rep):
        """
        Send a reply for a synchronous request sent by send_request.
        The first argument should be an instance of EventRequestBase.
        The second argument should be an instance of EventReplyBase.
        """

        assert isinstance(req, EventRequestBase)
        assert isinstance(rep, EventReplyBase)
        rep.dst = req.src
        if req.sync:
            req.reply_q.put(rep)
        else:
            self.send_event(rep.dst, rep)

    def close(self):
        """
        teardown method.
        The method name, close, is chosen for python context manager
        """
        pass


class AppManager(object):
    # singletone
    _instance = None

    @staticmethod
    def get_instance():
        if not AppManager._instance:
            AppManager._instance = AppManager()
        return AppManager._instance

    def __init__(self):
        self.applications_cls = {}
        self.applications = {}
        self.contexts_cls = {}
        self.contexts = {}

    def load_app(self, name):
        mod = utils.import_module(name)
        clses = inspect.getmembers(mod, lambda cls: (inspect.isclass(cls) and
                                                     issubclass(cls, RyuApp)))
        if clses:
            return clses[0][1]
        return None

    def load_apps(self, app_lists):
        app_lists = [app for app
                     in itertools.chain.from_iterable(app.split(',')
                                                      for app in app_lists)]
        while len(app_lists) > 0:
            app_cls_name = app_lists.pop(0)

            LOG.info('loading app %s', app_cls_name)

            cls = self.load_app(app_cls_name)
            if cls is None:
                continue

            self.applications_cls[app_cls_name] = cls

            services = []
            for key, context_cls in cls.context_iteritems():
                cls = self.contexts_cls.setdefault(key, context_cls)
                assert cls == context_cls

                if issubclass(context_cls, RyuApp):
                    services.extend(get_dependent_services(context_cls))

            services.extend(get_dependent_services(cls))
            if services:
                app_lists.extend(services)

    def create_contexts(self):
        for key, cls in self.contexts_cls.items():
            if issubclass(cls, RyuApp):
                # hack for dpset
                context = self._instantiate(None, cls)
            else:
                context = cls()
            LOG.info('creating context %s', key)
            assert not key in self.contexts
            self.contexts[key] = context
        return self.contexts

    def _update_bricks(self):
        for i in SERVICE_BRICKS.values():
            for _k, m in inspect.getmembers(i, inspect.ismethod):
                if not hasattr(m, 'observer'):
                    continue

                # name is module name of ev_cls
                name = m.observer.split('.')[-1]
                if name in SERVICE_BRICKS:
                    brick = SERVICE_BRICKS[name]
                    brick.register_observer(m.ev_cls, i.name, m.dispatchers)

                # allow RyuApp and Event class are in different module
                for brick in SERVICE_BRICKS.itervalues():
                    if m.ev_cls in brick._EVENTS:
                        brick.register_observer(m.ev_cls, i.name,
                                                m.dispatchers)

    @staticmethod
    def _report_brick(name, app):
        LOG.debug("BRICK %s" % name)
        for ev_cls, list_ in app.observers.items():
            LOG.debug("  PROVIDES %s TO %s" % (ev_cls.__name__, list_))
        for ev_cls in app.event_handlers.keys():
            LOG.debug("  CONSUMES %s" % (ev_cls.__name__,))

    @staticmethod
    def report_bricks():
        for brick, i in SERVICE_BRICKS.items():
            AppManager._report_brick(brick, i)

    def _instantiate(self, app_name, cls, *args, **kwargs):
        # for now, only single instance of a given module
        # Do we need to support multiple instances?
        # Yes, maybe for slicing.
        LOG.info('instantiating app %s of %s', app_name, cls.__name__)

        if hasattr(cls, 'OFP_VERSIONS') and not cls.OFP_VERSIONS is None:
            ofproto_protocol.set_app_supported_versions(cls.OFP_VERSIONS)

        if app_name is not None:
            assert app_name not in self.applications
        app = cls(*args, **kwargs)
        register_app(app)
        assert app.name not in self.applications
        self.applications[app.name] = app
        return app

    def instantiate(self, cls, *args, **kwargs):
        app = self._instantiate(None, cls, *args, **kwargs)
        self._update_bricks()
        self._report_brick(app.name, app)
        return app

    def instantiate_apps(self, *args, **kwargs):
        for app_name, cls in self.applications_cls.items():
            self._instantiate(app_name, cls, *args, **kwargs)

        self._update_bricks()
        self.report_bricks()

        threads = []
        for app in self.applications.values():
            t = app.start()
            if t is not None:
                threads.append(t)
        return threads

    @staticmethod
    def _close(app):
        close_method = getattr(app, 'close', None)
        if callable(close_method):
            close_method()

    def uninstantiate(self, name):
        app = self.applications.pop(name)
        unregister_app(app)
        for app_ in SERVICE_BRICKS.values():
            app_.unregister_observer_all_event(name)
        app.stop()
        self._close(app)
        events = app.events
        if not events.empty():
            app.logger.debug('%s events remians %d', app.name, events.qsize())

    def close(self):
        def close_all(close_dict):
            for app in close_dict.values():
                self._close(app)
            close_dict.clear()

        close_all(self.applications)
        close_all(self.contexts)
