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


class PluginManagerRPCWrapper(object):
    def __init__(self, plugin_manager: PluginManager):
        self.plugin_manager = plugin_manager
        self.rpc_server = RPCServer("PluginManager", "WeaveInstance Manager", [
            ServerAPI("list_plugins", "List plugins.", [],
                      self.plugin_manager.list_plugins),
            ServerAPI("activate_plugin", "Activate a plugin", [
                ArgParameter("plugin_url", "Plugin URL to activate", str),
            ], self.plugin_manager.activate),
            ServerAPI("deactivate_plugin", "Deactivate a plugin", [
                ArgParameter("plugin_url", "Plugin URL to deactivate", str),
            ], self.plugin_manager.deactivate),
            ServerAPI("enable_plugin", "Enable a plugin", [
                ArgParameter("plugin_url", "Plugin URL to deactivate", str),
            ], self.plugin_manager.enable_plugin),
            ServerAPI("disable_plugin", "Disable a plugin", [
                ArgParameter("plugin_url", "Plugin URL to deactivate", str),
            ], self.plugin_manager.disable_plugin),
            ServerAPI("install_plugin", "Install a plugin", [
                ArgParameter("plugin_url", "URL ending with .git.", str),
            ], self.plugin_manager.install),
            ServerAPI("uninstall_plugin", "Uninstall a plugin", [
                ArgParameter("plugin_url", "Plugin URL to uninstall", str),
            ], self.plugin_manager.uninstall),
            ServerAPI("plugin_info", "Get Plugin Info", [
                ArgParameter("plugin_url", "Get plugin info", str),
            ], self.plugin_manager.info),
        ], service)

    def start(self):
        self.rpc_server.start()

    def stop(self):
        self.rpc_server.stop()


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

    def start(self):
        self.plugin_manager.start()

        # Check if the messaging plugin is installed on any machine.
        try:
            messaging_db_plugin = PluginData.get(PluginData.app_url ==
                                                 MESSAGING_PLUGIN_URL)
        except DoesNotExist:
            print("No messaging plugin installed.")
            sys.exit(1)

        auth_token = self.instance_data.app_token
        if (messaging_db_plugin.machine.machine_id !=
                self.instance_data.machine_id):
            conn = WeaveConnection.discover()
        else:
            self.plugin_manager.load_plugin(messaging_db_plugin)
            self.plugin_manager.activate(MESSAGING_PLUGIN_URL, auth_token)
            conn = WeaveConnection.local()

        conn.connect()

        service = MessagingEnabled(auth_token=auth_token, conn=conn)

        self.registration_helper = PluginRegistrationHelper(service)
        self.registration_helper.start()

        plugin_tokens = []
        for plugin in self.instance_data.plugins:
            # We should have either started this above, or shouldn't be starting
            # at all.
            if plugin.app_url == MESSAGING_PLUGIN_URL:
                continue

            token = None
            if plugin.enabled:
                # TODO: try-except for register_plugin. Support loading plugin
                # in error state.
                token = self.registration_helper.register_plugin(plugin)

            plugin_tokens.append((plugin, token))

        self.plugin_manager.start_plugins(plugin_tokens)
        self.rpc_wrapper = PluginManagerRPCWrapper(self.plugin_manager,
                                                   self.registration_helper,
                                                   service, self.instance_data)
        self.rpc_wrapper.start()

    def stop(self):
        if self.rpc_wrapper:
            self.rpc_wrapper.stop()
        self.registration_helper.stop()
        self.plugin_manager.stop()
        self.stopped.set()

    def wait(self):
        self.stopped.wait()
