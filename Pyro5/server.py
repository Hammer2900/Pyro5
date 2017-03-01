"""
Server related classes (Daemon etc)

Pyro - Python Remote Objects.  Copyright by Irmen de Jong (irmen@razorvine.net).
"""

import inspect
import collections
import logging
import sys
import os
import time
import threading
import uuid
import warnings
import serpent
from . import errors, config, core, protocol, serializers
from .socketservers import multiplexserver, threadpoolserver
from .nameserver import locateNS

__all__ = ["Daemon", "callback", "expose", "behavior", "oneway"]

log = logging.getLogger("Pyro5.server")


def pyroObjectToAutoProxy(obj):
    """reduce function that automatically replaces Pyro objects by a Proxy"""
    daemon = getattr(obj, "_pyroDaemon", None)
    if daemon:
        # only return a proxy if the object is a registered pyro object
        return daemon.proxyFor(obj)
    return obj


_private_dunder_methods = frozenset([
    "__init__", "__call__", "__new__", "__del__", "__repr__", "__unicode__",
    "__str__", "__format__", "__nonzero__", "__bool__", "__coerce__",
    "__cmp__", "__eq__", "__ne__", "__hash__",
    "__dir__", "__enter__", "__exit__", "__copy__", "__deepcopy__", "__sizeof__",
    "__getattr__", "__setattr__", "__hasattr__", "__getattribute__", "__delattr__",
    "__instancecheck__", "__subclasscheck__", "__getinitargs__", "__getnewargs__",
    "__getstate__", "__setstate__", "__reduce__", "__reduce_ex__",
    "__getstate_for_dict__", "__setstate_from_dict__", "__subclasshook__"
])


def is_private_attribute(attr_name):
    """returns if the attribute name is to be considered private or not."""
    if attr_name in _private_dunder_methods:
        return True
    if not attr_name.startswith('_'):
        return False
    if len(attr_name) > 4 and attr_name.startswith("__") and attr_name.endswith("__"):
        return False
    return True


# decorators

def callback(method):
    """
    decorator to mark a method to be a 'callback'. This will make Pyro
    raise any errors also on the callback side, and not only on the side
    that does the callback call.
    """
    method._pyroCallback = True
    return method


def oneway(method):
    """
    decorator to mark a method to be oneway (client won't wait for a response)
    """
    method._pyroOneway = True
    return method


def expose(method_or_class):
    """
    Decorator to mark a method or class to be exposed for remote calls
    You can apply it to a method or a class as a whole.
    If you need to change the default instance mode or instance creator, also use a @behavior decorator.
    """
    if inspect.isdatadescriptor(method_or_class):
        func = method_or_class.fget or method_or_class.fset or method_or_class.fdel
        if is_private_attribute(func.__name__):
            raise AttributeError("exposing private names (starting with _) is not allowed")
        func._pyroExposed = True
        return method_or_class
    if is_private_attribute(method_or_class.__name__):
        raise AttributeError("exposing private names (starting with _) is not allowed")
    if inspect.isclass(method_or_class):
        clazz = method_or_class
        log.debug("exposing all members of %r", clazz)
        for name in clazz.__dict__:
            if is_private_attribute(name):
                continue
            thing = getattr(clazz, name)
            if inspect.isfunction(thing):
                thing._pyroExposed = True
            elif inspect.ismethod(thing):
                thing.__func__._pyroExposed = True
            elif inspect.isdatadescriptor(thing):
                if getattr(thing, "fset", None):
                    thing.fset._pyroExposed = True
                if getattr(thing, "fget", None):
                    thing.fget._pyroExposed = True
                if getattr(thing, "fdel", None):
                    thing.fdel._pyroExposed = True
        clazz._pyroExposed = True
        return clazz
    method_or_class._pyroExposed = True
    return method_or_class


def behavior(instance_mode="session", instance_creator=None):
    """
    Decorator to specify the server behavior of your Pyro class.
    """
    def _behavior(clazz):
        if not inspect.isclass(clazz):
            raise TypeError("behavior decorator can only be used on a class")
        if instance_mode not in ("single", "session", "percall"):
            raise ValueError("invalid instance mode: "+instance_mode)
        if instance_creator and not callable(instance_creator):
            raise TypeError("instance_creator must be a callable")
        clazz._pyroInstancing = (instance_mode, instance_creator)
        return clazz
    if not isinstance(instance_mode, str):
        raise SyntaxError("behavior decorator is missing argument(s)")
    return _behavior


@expose
class DaemonObject(object):
    """The part of the daemon that is exposed as a Pyro object."""

    def __init__(self, daemon):
        self.daemon = daemon

    def registered(self):
        """returns a list of all object names registered in this daemon"""
        return list(self.daemon.objectsById.keys())

    def ping(self):
        """a simple do-nothing method for testing purposes"""
        pass

    def info(self):
        """return some descriptive information about the daemon"""
        return "%s bound on %s, NAT %s, %d objects registered. Servertype: %s" % (
            core.DAEMON_NAME, self.daemon.locationStr, self.daemon.natLocationStr,
            len(self.daemon.objectsById), self.daemon.transportServer)

    def get_metadata(self, objectId, as_lists=False):
        """
        Get metadata for the given object (exposed methods, oneways, attributes).
        If you get an error in your proxy saying that 'DaemonObject' has no attribute 'get_metadata',
        you're probably connecting to an older Pyro version (4.26 or earlier).
        Either upgrade the Pyro version or set METADATA config item to False in your client code.
        """
        obj = self.daemon.objectsById.get(objectId)
        if obj is not None:
            metadata = get_exposed_members(obj, as_lists=as_lists)
            if not metadata["methods"] and not metadata["attrs"]:
                # Something seems wrong: nothing is remotely exposed.
                # Possibly because older code not using @expose is now running with a more recent Pyro version
                # where @expose is mandatory in the default configuration. Give a hint to the user.
                warnings.warn("Class %r doesn't expose any methods or attributes. Did you forget setting @expose on them?" % type(obj))
            return metadata
        else:
            log.debug("unknown object requested: %s", objectId)
            raise errors.DaemonError("unknown object")

    def get_next_stream_item(self, streamId):
        if streamId not in self.daemon.streaming_responses:
            raise errors.PyroError("item stream terminated")
        client, timestamp, linger_timestamp, stream = self.daemon.streaming_responses[streamId]
        if client is None:
            # reset client connection association (can be None if proxy disconnected)
            self.daemon.streaming_responses[streamId] = (core.current_context.client, timestamp, 0, stream)
        try:
            return next(stream)
        except Exception:
            del self.daemon.streaming_responses[streamId]
            raise

    def close_stream(self, streamId):
        if streamId in self.daemon.streaming_responses:
            del self.daemon.streaming_responses[streamId]


class Daemon(object):
    """
    Pyro daemon. Contains server side logic and dispatches incoming remote method calls
    to the appropriate objects.
    """

    def __init__(self, host=None, port=0, unixsocket=None, nathost=None, natport=None, interface=DaemonObject):
        if host is None:
            host = config.HOST
        if nathost is None:
            nathost = config.NATHOST
        if natport is None:
            natport = config.NATPORT or None
        if nathost and unixsocket:
            raise ValueError("cannot use nathost together with unixsocket")
        if (nathost is None) ^ (natport is None):
            raise ValueError("must provide natport with nathost")
        if config.SERVERTYPE == "thread":
            self.transportServer = threadpoolserver.SocketServer_Threadpool()
        elif config.SERVERTYPE == "multiplex":
            self.transportServer = multiplexserver.SocketServer_Multiplex()
        else:
            raise errors.PyroError("invalid server type '%s'" % config.SERVERTYPE)
        self.transportServer.init(self, host, port, unixsocket)
        #: The location (str of the form ``host:portnumber``) on which the Daemon is listening
        self.locationStr = self.transportServer.locationStr
        log.debug("created daemon on %s (pid %d)", self.locationStr, os.getpid())
        natport_for_loc = natport
        if natport == 0:
            # expose internal port number as NAT port as well. (don't use port because it could be 0 and will be chosen by the OS)
            natport_for_loc = int(self.locationStr.split(":")[1])
        #: The NAT-location (str of the form ``nathost:natportnumber``) on which the Daemon is exposed for use with NAT-routing
        self.natLocationStr = "%s:%d" % (nathost, natport_for_loc) if nathost else None
        if self.natLocationStr:
            log.debug("NAT address is %s", self.natLocationStr)
        pyroObject = interface(self)
        pyroObject._pyroId = core.DAEMON_NAME
        #: Dictionary from Pyro object id to the actual Pyro object registered by this id
        self.objectsById = {pyroObject._pyroId: pyroObject}
        self.__mustshutdown = threading.Event()
        self.__loopstopped = threading.Event()
        self.__loopstopped.set()
        # assert that the configured serializers are available, and remember their ids:
        self.__serializer_ids = {serializers.get_serializer(ser_name).serializer_id for ser_name in config.SERIALIZERS_ACCEPTED}
        log.debug("accepted serializers: %s" % config.SERIALIZERS_ACCEPTED)
        log.debug("pyro protocol version: %d" % protocol.PROTOCOL_VERSION)
        self._pyroInstances = {}   # pyro objects for instance_mode=single (singletons, just one per daemon)
        self.streaming_responses = {}   # stream_id -> (client, creation_timestamp, linger_timestamp, stream)
        self.housekeeper_lock = threading.Lock()

    @property
    def sock(self):
        """the server socket used by the daemon"""
        return self.transportServer.sock

    @property
    def sockets(self):
        """list of all sockets used by the daemon (server socket and all active client sockets)"""
        return self.transportServer.sockets

    @property
    def selector(self):
        """the multiplexing selector used, if using the multiplex server type"""
        return self.transportServer.selector

    @staticmethod
    def serveSimple(objects, host=None, port=0, daemon=None, ns=True, verbose=True):
        """
        Basic method to fire up a daemon (or supply one yourself).
        objects is a dict containing objects to register as keys, and
        their names (or None) as values. If ns is true they will be registered
        in the naming server as well, otherwise they just stay local.
        If you need to publish on a unix domain socket you can't use this shortcut method.
        See the documentation on 'publishing objects' (in chapter: Servers) for more details.
        """
        if daemon is None:
            daemon = Daemon(host, port)
        with daemon:
            if ns:
                ns = locateNS()
            for obj, name in objects.items():
                if ns:
                    localname = None  # name is used for the name server
                else:
                    localname = name  # no name server, use name in daemon
                uri = daemon.register(obj, localname)
                if verbose:
                    print("Object {0}:\n    uri = {1}".format(repr(obj), uri))
                if name and ns:
                    ns.register(name, uri)
                    if verbose:
                        print("    name = {0}".format(name))
            if verbose:
                print("Pyro daemon running.")
            daemon.requestLoop()

    def requestLoop(self, loopCondition=lambda: True):
        """
        Goes in a loop to service incoming requests, until someone breaks this
        or calls shutdown from another thread.
        """
        self.__mustshutdown.clear()
        log.info("daemon %s entering requestloop", self.locationStr)
        try:
            self.__loopstopped.clear()
            condition = lambda: not self.__mustshutdown.isSet() and loopCondition()
            self.transportServer.loop(loopCondition=condition)
        finally:
            self.__loopstopped.set()
        log.debug("daemon exits requestloop")

    def events(self, eventsockets):
        """for use in an external event loop: handle any requests that are pending for this daemon"""
        return self.transportServer.events(eventsockets)

    def shutdown(self):
        """Cleanly terminate a daemon that is running in the requestloop."""
        log.debug("daemon shutting down")
        self.streaming_responses = {}
        time.sleep(0.02)
        self.__mustshutdown.set()
        if self.transportServer:
            self.transportServer.shutdown()
            time.sleep(0.02)
        self.close()
        self.__loopstopped.wait(timeout=5)  # use timeout to avoid deadlock situations

    @property
    def _shutting_down(self):
        return self.__mustshutdown.is_set()

    def _handshake(self, conn, denied_reason=None):
        """
        Perform connection handshake with new clients.
        Client sends a MSG_CONNECT message with a serialized data payload.
        If all is well, return with a CONNECT_OK message.
        The reason we're not doing this with a MSG_INVOKE method call on the daemon
        (like when retrieving the metadata) is because we need to force the clients
        to get past an initial connect handshake before letting them invoke any method.
        Return True for successful handshake, False if something was wrong.
        If a denied_reason is given, the handshake will fail with the given reason.
        """
        serializer_id = serializers.MarshalSerializer.serializer_id
        msg_seq = 0
        try:
            msg = protocol.recv_stub(conn, [protocol.MSG_CONNECT])
            msg_seq = msg.seq
            if denied_reason:
                raise Exception(denied_reason)
            if config.LOGWIRE:
                core.log_wiredata(log, "daemon handshake received", msg)
            if msg.serializer_id not in self.__serializer_ids:
                raise errors.SerializationError("message used serializer that is not accepted: %d" % msg.serializer_id)
            if "CORR" in msg.annotations:
                core.current_context.correlation_id = uuid.UUID(bytes=msg.annotations["CORR"])
            else:
                core.current_context.correlation_id = uuid.uuid4()
            serializer_id = msg.serializer_id
            serializer = serializers.get_serializer_by_id(serializer_id)
            data = serializer.deserializeData(msg.data, msg.flags & protocol.FLAGS_COMPRESSED)
            handshake_response = self.validateHandshake(conn, data["handshake"])
            # Getting the metadata is done by including the object metadata
            # in the handshake response. This avoids a separate remote call to get_metadata.
            handshake_response = {
                "handshake": handshake_response,
                "meta": self.objectsById[core.DAEMON_NAME].get_metadata(data["object"], as_lists=True)
            }
            flags = 0
            data, compressed = serializer.serializeData(handshake_response, config.COMPRESSION)
            msgtype = protocol.MSG_CONNECTOK
            if compressed:
                flags |= protocol.FLAGS_COMPRESSED
        except errors.ConnectionClosedError:
            log.debug("handshake failed, connection closed early")
            return False
        except Exception as x:
            log.debug("handshake failed, reason:", exc_info=True)
            serializer = serializers.get_serializer_by_id(serializer_id)
            data, compressed = serializer.serializeData(str(x), False)
            msgtype = protocol.MSG_CONNECTFAIL
            flags = protocol.FLAGS_COMPRESSED if compressed else 0
        # We need a minimal amount of response data or the socket will remain blocked
        # on some systems... (messages smaller than 40 bytes)
        msg = protocol.SendingMessage(msgtype, flags, msg_seq, serializer_id, data, annotations=self.annotations())
        if config.LOGWIRE:
            core.log_wiredata(log, "daemon handshake response", msg)
        conn.send(msg.data)
        return msg.type == protocol.MSG_CONNECTOK

    def validateHandshake(self, conn, data):
        """
        Override this to create a connection validator for new client connections.
        It should return a response data object normally if the connection is okay,
        or should raise an exception if the connection should be denied.
        """
        return "hello"

    def clientDisconnect(self, conn):
        """
        Override this to handle a client disconnect.
        Conn is the SocketConnection object that was disconnected.
        """
        pass

    def handleRequest(self, conn):
        """
        Handle incoming Pyro request. Catches any exception that may occur and
        wraps it in a reply to the calling side, as to not make this server side loop
        terminate due to exceptions caused by remote invocations.
        """
        request_flags = 0
        request_seq = 0
        request_serializer_id = serializers.MarshalSerializer.serializer_id
        wasBatched = False
        isCallback = False
        try:
            msg = protocol.recv_stub(conn, [protocol.MSG_INVOKE, protocol.MSG_PING])
        except errors.CommunicationError as x:
            # we couldn't even get data from the client, this is an immediate error
            # log.info("error receiving data from client %s: %s", conn.sock.getpeername(), x)
            raise x
        try:
            request_flags = msg.flags
            request_seq = msg.seq
            request_serializer_id = msg.serializer_id
            if "CORR" in msg.annotations:
                core.current_context.correlation_id = uuid.UUID(bytes=msg.annotations["CORR"])
            else:
                core.current_context.correlation_id = uuid.uuid4()
            if config.LOGWIRE:
                core.log_wiredata(log, "daemon wiredata received", msg)
            if msg.type == protocol.MSG_PING:
                # return same seq, but ignore any data (it's a ping, not an echo). Nothing is deserialized.
                msg = protocol.SendingMessage(protocol.MSG_PING, 0, msg.seq, msg.serializer_id, b"pong", annotations=self.annotations())
                if config.LOGWIRE:
                    core.log_wiredata(log, "daemon wiredata sending", msg)
                conn.send(msg.data)
                return
            if msg.serializer_id not in self.__serializer_ids:
                raise errors.SerializationError("message used serializer that is not accepted: %d" % msg.serializer_id)
            serializer = serializers.get_serializer_by_id(msg.serializer_id)
            # normal deserialization of remote call arguments
            objId, method, vargs, kwargs = serializer.deserializeCall(msg.data, compressed=msg.flags & protocol.FLAGS_COMPRESSED)
            core.current_context.client = conn
            core.current_context.client_sock_addr = conn.sock.getpeername()   # store this because on oneway calls the socket will be disconnected
            core.current_context.seq = msg.seq
            core.current_context.annotations = msg.annotations
            core.current_context.msg_flags = msg.flags
            core.current_context.serializer_id = msg.serializer_id
            del msg  # invite GC to collect the object, don't wait for out-of-scope
            obj = self.objectsById.get(objId)
            if obj is not None:
                if inspect.isclass(obj):
                    obj = self._getInstance(obj, conn)
                if request_flags & protocol.FLAGS_BATCH:
                    # batched method calls, loop over them all and collect all results
                    data = []
                    for method, vargs, kwargs in vargs:
                        method = util.getAttribute(obj, method)
                        try:
                            result = method(*vargs, **kwargs)  # this is the actual method call to the Pyro object
                        except Exception:
                            xt, xv = sys.exc_info()[0:2]
                            log.debug("Exception occurred while handling batched request: %s", xv)
                            xv._pyroTraceback = errors.formatTraceback(detailed=config.DETAILED_TRACEBACK)
                            data.append(futures._ExceptionWrapper(xv))
                            break  # stop processing the rest of the batch
                        else:
                            data.append(result)    # note that we don't support streaming results in batch mode
                    wasBatched = True
                else:
                    # normal single method call
                    if method == "__getattr__":
                        # special case for direct attribute access (only exposed @properties are accessible)
                        data = get_exposed_property_value(obj, vargs[0])
                    elif method == "__setattr__":
                        # special case for direct attribute access (only exposed @properties are accessible)
                        data = set_exposed_property_value(obj, vargs[0], vargs[1])
                    else:
                        method = util.getAttribute(obj, method)
                        if request_flags & protocol.FLAGS_ONEWAY and config.ONEWAY_THREADED:
                            # oneway call to be run inside its own thread
                            _OnewayCallThread(target=method, args=vargs, kwargs=kwargs).start()
                        else:
                            isCallback = getattr(method, "_pyroCallback", False)
                            data = method(*vargs, **kwargs)  # this is the actual method call to the Pyro object
                            if not request_flags & protocol.FLAGS_ONEWAY:
                                isStream, data = self._streamResponse(data, conn)
                                if isStream:
                                    # throw an exception as well as setting message flags
                                    # this way, it is backwards compatible with older pyro versions.
                                    exc = errors.ProtocolError("result of call is an iterator")
                                    ann = {"STRM": data.encode()} if data else {}
                                    self._sendExceptionResponse(conn, request_seq, serializer.serializer_id, exc, None,
                                                                annotations=ann, flags=protocol.FLAGS_ITEMSTREAMRESULT)
                                    return
            else:
                log.debug("unknown object requested: %s", objId)
                raise errors.DaemonError("unknown object")
            if request_flags & protocol.FLAGS_ONEWAY:
                return  # oneway call, don't send a response
            else:
                data, compressed = serializer.serializeData(data, compress=config.COMPRESSION)
                response_flags = 0
                if compressed:
                    response_flags |= protocol.FLAGS_COMPRESSED
                if wasBatched:
                    response_flags |= protocol.FLAGS_BATCH
                msg = protocol.SendingMessage(protocol.MSG_RESULT, response_flags, request_seq, serializer.serializer_id, data, annotations=self.annotations())
                if config.LOGWIRE:
                    core.log_wiredata(log, "daemon wiredata sending", msg)
                conn.send(msg.data)
        except Exception:
            xt, xv = sys.exc_info()[0:2]
            msg = getattr(xv, "pyroMsg", None)
            if msg:
                request_seq = msg.seq
                request_serializer_id = msg.serializer_id
            if xt is not errors.ConnectionClosedError:
                log.debug("Exception occurred while handling request: %r", xv)
                if not request_flags & protocol.FLAGS_ONEWAY:
                    if isinstance(xv, errors.SerializationError) or not isinstance(xv, errors.CommunicationError):
                        # only return the error to the client if it wasn't a oneway call, and not a communication error
                        # (in these cases, it makes no sense to try to report the error back to the client...)
                        tblines = errors.formatTraceback(detailed=config.DETAILED_TRACEBACK)
                        self._sendExceptionResponse(conn, request_seq, request_serializer_id, xv, tblines)
            if isCallback or isinstance(xv, (errors.CommunicationError, errors.SecurityError)):
                raise  # re-raise if flagged as callback, communication or security error.

    def _clientDisconnect(self, conn):
        if config.ITER_STREAM_LINGER > 0:
            # client goes away, keep streams around for a bit longer (allow reconnect)
            for streamId in list(self.streaming_responses):
                info = self.streaming_responses.get(streamId, None)
                if info and info[0] is conn:
                    _, timestamp, _, stream = info
                    self.streaming_responses[streamId] = (None, timestamp, time.time(), stream)
        else:
            # client goes away, close any streams it had open as well
            for streamId in list(self.streaming_responses):
                info = self.streaming_responses.get(streamId, None)
                if info and info[0] is conn:
                    del self.streaming_responses[streamId]
        self.clientDisconnect(conn)  # user overridable hook

    def _housekeeping(self):
        """
        Perform periodical housekeeping actions (cleanups etc)
        """
        if self._shutting_down:
            return
        with self.housekeeper_lock:
            if self.streaming_responses:
                if config.ITER_STREAM_LIFETIME > 0:
                    # cleanup iter streams that are past their lifetime
                    for streamId in list(self.streaming_responses.keys()):
                        info = self.streaming_responses.get(streamId, None)
                        if info:
                            last_use_period = time.time() - info[1]
                            if 0 < config.ITER_STREAM_LIFETIME < last_use_period:
                                del self.streaming_responses[streamId]
                if config.ITER_STREAM_LINGER > 0:
                    # cleanup iter streams that are past their linger time
                    for streamId in list(self.streaming_responses.keys()):
                        info = self.streaming_responses.get(streamId, None)
                        if info and info[2]:
                            linger_period = time.time() - info[2]
                            if linger_period > config.ITER_STREAM_LINGER:
                                del self.streaming_responses[streamId]
            self.housekeeping()

    def housekeeping(self):
        """
        Override this to add custom periodic housekeeping (cleanup) logic.
        This will be called every few seconds by the running daemon's request loop.
        """
        pass

    def _getInstance(self, clazz, conn):
        """
        Find or create a new instance of the class
        """
        def createInstance(clazz, creator):
            try:
                if creator:
                    obj = creator(clazz)
                    if isinstance(obj, clazz):
                        return obj
                    raise TypeError("instance creator returned object of different type")
                return clazz()
            except Exception:
                log.exception("could not create pyro object instance")
                raise
        instance_mode, instance_creator = clazz._pyroInstancing
        if instance_mode == "single":
            # create and use one singleton instance of this class (not a global singleton, just exactly one per daemon)
            instance = self._pyroInstances.get(clazz)
            if not instance:
                log.debug("instancemode %s: creating new pyro object for %s", instance_mode, clazz)
                instance = createInstance(clazz, instance_creator)
                self._pyroInstances[clazz] = instance
            return instance
        elif instance_mode == "session":
            # Create and use one instance for this proxy connection
            # the instances are kept on the connection object.
            # (this is the default instance mode when using new style @expose)
            instance = conn.pyroInstances.get(clazz)
            if not instance:
                log.debug("instancemode %s: creating new pyro object for %s", instance_mode, clazz)
                instance = createInstance(clazz, instance_creator)
                conn.pyroInstances[clazz] = instance
            return instance
        elif instance_mode == "percall":
            # create and use a new instance just for this call
            log.debug("instancemode %s: creating new pyro object for %s", instance_mode, clazz)
            return createInstance(clazz, instance_creator)
        else:
            raise errors.DaemonError("invalid instancemode in registered class")

    def _sendExceptionResponse(self, connection, seq, serializer_id, exc_value, tbinfo, flags=0, annotations={}):
        """send an exception back including the local traceback info"""
        exc_value._pyroTraceback = tbinfo
        serializer = serializers.get_serializer_by_id(serializer_id)
        try:
            data, compressed = serializer.serializeData(exc_value)
        except:
            # the exception object couldn't be serialized, use a generic PyroError instead
            xt, xv, tb = sys.exc_info()
            msg = "Error serializing exception: %s. Original exception: %s: %s" % (str(xv), type(exc_value), str(exc_value))
            exc_value = errors.PyroError(msg)
            exc_value._pyroTraceback = tbinfo
            data, compressed = serializer.serializeData(exc_value)
        flags |= protocol.FLAGS_EXCEPTION
        if compressed:
            flags |= protocol.FLAGS_COMPRESSED
        ann = self.annotations()
        ann.update(annotations or {})
        msg = protocol.SendingMessage(protocol.MSG_RESULT, flags, seq, serializer_id, data, annotations=ann)
        if config.LOGWIRE:
            core.log_wiredata(log, "daemon wiredata sending (error response)", msg)
        connection.send(msg.data)

    def register(self, obj_or_class, objectId=None, force=False):
        """
        Register a Pyro object under the given id. Note that this object is now only
        known inside this daemon, it is not automatically available in a name server.
        This method returns a URI for the registered object.
        Pyro checks if an object is already registered, unless you set force=True.
        You can register a class or an object (instance) directly.
        For a class, Pyro will create instances of it to handle the remote calls according
        to the instance_mode (set via @expose on the class). The default there is one object
        per session (=proxy connection). If you register an object directly, Pyro will use
        that single object for *all* remote calls.
        """
        if objectId:
            if not isinstance(objectId, str):
                raise TypeError("objectId must be a string or None")
        else:
            objectId = "obj_" + uuid.uuid4().hex  # generate a new objectId
        if inspect.isclass(obj_or_class):
            if not hasattr(obj_or_class, "_pyroInstancing"):
                obj_or_class._pyroInstancing = ("session", None)
        if not force:
            if hasattr(obj_or_class, "_pyroId") and obj_or_class._pyroId != "":  # check for empty string is needed for Cython
                raise errors.DaemonError("object or class already has a Pyro id")
            if objectId in self.objectsById:
                raise errors.DaemonError("an object or class is already registered with that id")
        # set some pyro attributes
        obj_or_class._pyroId = objectId
        obj_or_class._pyroDaemon = self
        if config.AUTOPROXY:
            # register a custom serializer for the type to automatically return proxies
            # we need to do this for all known serializers
            for ser in serializers._serializers.values():
                ser.register_type_replacement(type(obj_or_class), pyroObjectToAutoProxy)
        # register the object/class in the mapping
        self.objectsById[obj_or_class._pyroId] = obj_or_class
        return self.uriFor(objectId)

    def unregister(self, objectOrId):
        """
        Remove a class or object from the known objects inside this daemon.
        You can unregister the class/object directly, or with its id.
        """
        if objectOrId is None:
            raise ValueError("object or objectid argument expected")
        if not isinstance(objectOrId, str):
            objectId = getattr(objectOrId, "_pyroId", None)
            if objectId is None:
                raise errors.DaemonError("object isn't registered")
        else:
            objectId = objectOrId
            objectOrId = None
        if objectId == core.DAEMON_NAME:
            return
        if objectId in self.objectsById:
            del self.objectsById[objectId]
            if objectOrId is not None:
                del objectOrId._pyroId
                del objectOrId._pyroDaemon
                # Don't remove the custom type serializer because there may be
                # other registered objects of the same type still depending on it.

    def uriFor(self, objectOrId, nat=True):
        """
        Get a URI for the given object (or object id) from this daemon.
        Only a daemon can hand out proper uris because the access location is
        contained in them.
        Note that unregistered objects cannot be given an uri, but unregistered
        object names can (it's just a string we're creating in that case).
        If nat is set to False, the configured NAT address (if any) is ignored and it will
        return an URI for the internal address.
        """
        if not isinstance(objectOrId, str):
            objectOrId = getattr(objectOrId, "_pyroId", None)
            if objectOrId is None or objectOrId not in self.objectsById:
                raise errors.DaemonError("object isn't registered in this daemon")
        if nat:
            loc = self.natLocationStr or self.locationStr
        else:
            loc = self.locationStr
        return URI("PYRO:%s@%s" % (objectOrId, loc))

    def resetMetadataCache(self, objectOrId, nat=True):
        """Reset cache of metadata when a Daemon has available methods/attributes
        dynamically updated.  Clients will have to get a new proxy to see changes"""
        uri = self.uriFor(objectOrId, nat)
        # can only be cached if registered, else no-op
        if uri.object in self.objectsById:
            registered_object = self.objectsById[uri.object]
            # Clear cache regardless of how it is accessed
            reset_exposed_members(registered_object, as_lists=True)
            reset_exposed_members(registered_object, as_lists=False)

    def proxyFor(self, objectOrId, nat=True):
        """
        Get a fully initialized Pyro Proxy for the given object (or object id) for this daemon.
        If nat is False, the configured NAT address (if any) is ignored.
        The object or id must be registered in this daemon, or you'll get an exception.
        (you can't get a proxy for an unknown object)
        """
        uri = self.uriFor(objectOrId, nat)
        proxy = Proxy(uri)
        try:
            registered_object = self.objectsById[uri.object]
        except KeyError:
            raise errors.DaemonError("object isn't registered in this daemon")
        meta = get_exposed_members(registered_object)
        proxy._pyroGetMetadata(known_metadata=meta)
        return proxy

    def close(self):
        """Close down the server and release resources"""
        self.__mustshutdown.set()
        self.streaming_responses = {}
        if self.transportServer:
            log.debug("daemon closing")
            self.transportServer.close()
            self.transportServer = None

    def annotations(self):
        """
        Returns a dict with annotations to be sent with each message.
        Default behavior is to include the correlation id from the current context (if it is set).
        If you override this, don't forget to call the original method and add to the dictionary returned from it,
        rather than simply returning a new dictionary.
        """
        if core.current_context.correlation_id:
            return {"CORR": core.current_context.correlation_id.bytes}
        return {}

    def combine(self, daemon):
        """
        Combines the event loop of the other daemon in the current daemon's loop.
        You can then simply run the current daemon's requestLoop to serve both daemons.
        This works fine on the multiplex server type, but doesn't work with the threaded server type.
        """
        log.debug("combining event loop with other daemon")
        self.transportServer.combine_loop(daemon.transportServer)

    def __repr__(self):
        if hasattr(self, "locationStr"):
            return "<%s.%s at 0x%x; %s; %d objects>" % (self.__class__.__module__, self.__class__.__name__,
                                                        id(self), self.locationStr, len(self.objectsById))
        else:
            # daemon objects may come back from serialized form without being properly initialized (by design)
            return "<%s.%s at 0x%x; unusable>" % (self.__class__.__module__, self.__class__.__name__, id(self))

    def __enter__(self):
        if not self.transportServer:
            raise errors.PyroError("cannot reuse this object")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __getstate__(self):
        # A little hack to make it possible to serialize Pyro objects, because they can reference a daemon,
        # but it is not meant to be able to properly serialize/deserialize Daemon objects.
        return {}

    def __getstate_for_dict__(self):
        return tuple(self.__getstate__())

    def __setstate_from_dict__(self, state):
        pass

    if sys.version_info < (3, 0):
        __lazy_dict_iterator_types = (type({}.iterkeys()), type({}.itervalues()), type({}.iteritems()))
    else:
        __lazy_dict_iterator_types = (type({}.keys()), type({}.values()), type({}.items()))

    def _streamResponse(self, data, client):
        if isinstance(data, collections.Iterator) or inspect.isgenerator(data):
            if config.ITER_STREAMING:
                if type(data) in self.__lazy_dict_iterator_types:
                    raise errors.PyroError("won't serialize or stream lazy dict iterators, convert to list yourself")
                stream_id = str(uuid.uuid4())
                self.streaming_responses[stream_id] = (client, time.time(), 0, data)
                return True, stream_id
            return True, None
        return False, data


# register the special serializers for the pyro objects
serpent.register_class(Daemon, serializers.pyro_class_serpent_serializer)
serializers.SerializerBase.register_class_to_dict(Daemon, serializers.serialize_pyro_object_to_dict, serpent_too=False)


class _OnewayCallThread(threading.Thread):
    def __init__(self, target, args, kwargs):
        super(_OnewayCallThread, self).__init__(target=target, args=args, kwargs=kwargs, name="oneway-call")
        self.daemon = True
        self.parent_context = core.current_context.to_global()

    def run(self):
        core.current_context.from_global(self.parent_context)
        super(_OnewayCallThread, self).run()


__exposed_member_cache = {}


def reset_exposed_members(obj, only_exposed=True, as_lists=False):
    """Delete any cached exposed members forcing recalculation on next request"""
    if not inspect.isclass(obj):
        obj = obj.__class__
    cache_key = (obj, only_exposed, as_lists)
    __exposed_member_cache.pop(cache_key, None)


def get_exposed_members(obj, only_exposed=True, as_lists=False, use_cache=True):
    """
    Return public and exposed members of the given object's class.
    You can also provide a class directly.
    Private members are ignored no matter what (names starting with underscore).
    If only_exposed is True, only members tagged with the @expose decorator are
    returned. If it is False, all public members are returned.
    The return value consists of the exposed methods, exposed attributes, and methods
    tagged as @oneway.
    (All this is used as meta data that Pyro sends to the proxy if it asks for it)
    as_lists is meant for python 2 compatibility.
    """
    if not inspect.isclass(obj):
        obj = obj.__class__

    cache_key = (obj, only_exposed, as_lists)
    if use_cache and cache_key in __exposed_member_cache:
        return __exposed_member_cache[cache_key]

    methods = set()  # all methods
    oneway = set()  # oneway methods
    attrs = set()  # attributes
    for m in dir(obj):      # also lists names inherited from super classes
        if is_private_attribute(m):
            continue
        v = getattr(obj, m)
        if inspect.ismethod(v) or inspect.isfunction(v):
            if getattr(v, "_pyroExposed", not only_exposed):
                methods.add(m)
                # check if the method is marked with the 'oneway' decorator:
                if getattr(v, "_pyroOneway", False):
                    oneway.add(m)
        elif inspect.isdatadescriptor(v):
            func = getattr(v, "fget", None) or getattr(v, "fset", None) or getattr(v, "fdel", None)
            if func is not None and getattr(func, "_pyroExposed", not only_exposed):
                attrs.add(m)
        # Note that we don't expose plain class attributes no matter what.
        # it is a syntax error to add a decorator on them, and it is not possible
        # to give them a _pyroExposed tag either.
        # The way to expose attributes is by using properties for them.
        # This automatically solves the protection/security issue: you have to
        # explicitly decide to make an attribute into a @property (and to @expose it)
        # before it is remotely accessible.
    if as_lists:
        methods = list(methods)
        oneway = list(oneway)
        attrs = list(attrs)
    result = {
        "methods": methods,
        "oneway": oneway,
        "attrs": attrs
    }
    __exposed_member_cache[cache_key] = result
    return result


def get_exposed_property_value(obj, propname, only_exposed=True):
    """
    Return the value of an @exposed @property.
    If the requested property is not a @property or not exposed,
    an AttributeError is raised instead.
    """
    v = getattr(obj.__class__, propname)
    if inspect.isdatadescriptor(v):
        if v.fget and getattr(v.fget, "_pyroExposed", not only_exposed):
            return v.fget(obj)
    raise AttributeError("attempt to access unexposed or unknown remote attribute '%s'" % propname)


def set_exposed_property_value(obj, propname, value, only_exposed=True):
    """
    Sets the value of an @exposed @property.
    If the requested property is not a @property or not exposed,
    an AttributeError is raised instead.
    """
    v = getattr(obj.__class__, propname)
    if inspect.isdatadescriptor(v):
        pfunc = v.fget or v.fset or v.fdel
        if v.fset and getattr(pfunc, "_pyroExposed", not only_exposed):
            return v.fset(obj, value)
    raise AttributeError("attempt to access unexposed or unknown remote attribute '%s'" % propname)