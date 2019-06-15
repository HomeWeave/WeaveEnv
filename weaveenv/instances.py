import os
import sys
from threading import Event

from peewee import DoesNotExist

from weavelib.exceptions import ObjectNotFound
from weavelib.messaging import WeaveConnection
from weavelib.rpc import RPCServer, ServerAPI, ArgParameter, RPCClient
from weavelib.rpc import find_rpc
from weavelib.services.service_base import MessagingEnabled

from .database import PluginData
from .plugins import url_to_plugin_id


MESSAGING_PLUGIN_URL = "https://github.com/HomeWeave/WeaveServer.git"


def load_installed_plugins(db_plugins, service):
    conn = service.get_connection()
    token = service.get_auth_token()

    rpc_info = find_rpc(service, MESSAGING_PLUGIN_URL, "app_manager")
    client = RPCClient(conn, rpc_info, token)

    def register_plugin(plugin):
        return client["register_plugin"](plugin.plugin_id(), plugin.name,
                                         plugin.src, _block=True)

    messaging_app_id = url_to_plugin_id(MESSAGING_PLUGIN_URL)
    plugins = [x for x in db_plugins if x.app_id != messaging_app_id]
    return [(x, register_plugin(x) if x.enabled else None) for x in plugins]


def get_plugin_by_id(plugin_id):
    try:
        return PluginData.get(PluginData.app_id == plugin_id)
    except DoesNotExist:
        raise ObjectNotFound(plugin_id)


def get_plugin_by_url(url):
    return get_plugin_by_id(url_to_plugin_id(url))


class PluginManagerRPCWrapper(object):
    def __init__(self, plugin_manager, service):
        self.plugin_manager = plugin_manager
        self.rpc_server = RPCServer("PluginManager", "WeaveInstance Manager", [
            ServerAPI("list_plugins", "List plugins.", [],
                      self.list_plugins),
            ServerAPI("activate_plugin", "Activate a plugin", [
                ArgParameter("plugin_url", "PluginID to activate", str),
            ], self.plugin_manager.activate),
            ServerAPI("deactivate_plugin", "Deactivate a plugin", [
                ArgParameter("plugin_url", "PluginID to deactivate", str),
            ], self.plugin_manager.deactivate),
            ServerAPI("install_plugin", "Install a plugin", [
                ArgParameter("plugin_url", "URL ending with .git.", str),
            ], self.plugin_manager.install),
            ServerAPI("uninstall_plugin", "Uninstall a plugin", [
                ArgParameter("plugin_url", "PluginID to uninstall", str),
            ], self.plugin_manager.uninstall),
        ], service)

    def start(self):
        self.rpc_server.start()

    def stop(self):
        self.rpc_server.stop()

    def list_plugins(self):
        return [x.info() for x in self.plugin_manager.list()]


class BaseWeaveEnvInstance(object):
    def start(self):
        raise NotImplementedError

    def list_plugins(self):
        raise NotImplementedError

    def activate(self, plugin_id):
        raise NotImplementedError

    def deactivate(self, plugin_id):
        raise NotImplementedError

    def install(self, plugin_url):
        raise NotImplementedError

    def uninstall(self, plugin_id):
        raise NotImplementedError


class LocalWeaveInstance(BaseWeaveEnvInstance):
    def __init__(self, instance_data, plugin_manager):
        self.instance_data = instance_data
        self.plugin_manager = plugin_manager
        self.stopped = Event()
        self.rpc_wrapper = None

    def start(self):
        self.plugin_manager.start()

        # Insert basic data into the DB such as command-line access Data and
        # current machine data.
        # Check if the messaging plugin is installed any machine.
        try:
            messaging_db_plugin = get_plugin_by_url(MESSAGING_PLUGIN_URL)
        except ObjectNotFound:
            print("No messaging plugin installed.")
            sys.exit(1)

        auth_token = self.instance_data.app_token
        if (messaging_db_plugin.machine.machine_id !=
                self.instance_data.machine_id):
            conn = WeaveConnection.discover()
        else:
            self.plugin_manager.load_plugin(messaging_db_plugin, auth_token)
            self.plugin_manager.activate(MESSAGING_PLUGIN_URL)
            conn = WeaveConnection.local()

        conn.connect()

        service = MessagingEnabled(auth_token=auth_token, conn=conn)
        plugin_tokens = load_installed_plugins(self.instance_data.plugins,
                                               service)
        self.plugin_manager.start_plugins(plugin_tokens)
        self.rpc_wrapper = PluginManagerRPCWrapper(self.plugin_manager, service)
        self.rpc_wrapper.start()

    def stop(self):
        if self.rpc_wrapper:
            self.rpc_wrapper.stop()
        self.plugin_manager.stop()
        self.stopped.set()

    def wait(self):
        self.stopped.wait()
