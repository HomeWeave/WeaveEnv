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


class PluginRegistrationHelper(object):
    def __init__(self, service):
        self.service = service
        self.client = None

    def start(self):
        rpc_info = find_rpc(self.service, MESSAGING_PLUGIN_URL, "app_manager")
        self.client = RPCClient(self.service.get_connection(), rpc_info,
                                self.service.get_auth_token())
        self.client.start()

    def stop(self):
        self.client.stop()

    def register_plugin(self, db_plugin):
        return self.client["register_plugin"](db_plugin.name, db_plugin.app_url,
                                              _block=True)

    def unregister_plugin(self, plugin_url):
        return self.client["unregister_plugin"](plugin_url, _block=True)


class PluginManagerRPCWrapper(object):
    def __init__(self, plugin_manager, registration_helper, service,
                 instance_data):
        self.plugin_manager = plugin_manager
        self.registration_helper = registration_helper
        self.instance_data = instance_data
        self.rpc_server = RPCServer("PluginManager", "WeaveInstance Manager", [
            ServerAPI("list_plugins", "List plugins.", [], self.list_plugins),
            ServerAPI("activate_plugin", "Activate a plugin", [
                ArgParameter("plugin_url", "Plugin URL to activate", str),
            ], self.activate),
            ServerAPI("deactivate_plugin", "Deactivate a plugin", [
                ArgParameter("plugin_url", "Plugin URL to deactivate", str),
            ], self.deactivate),
            ServerAPI("enable_plugin", "Enable a plugin", [
                ArgParameter("plugin_url", "Plugin URL to deactivate", str),
            ], self.enable_plugin),
            ServerAPI("disable_plugin", "Disable a plugin", [
                ArgParameter("plugin_url", "Plugin URL to deactivate", str),
            ], self.disable_plugin),
            ServerAPI("install_plugin", "Install a plugin", [
                ArgParameter("plugin_url", "URL ending with .git.", str),
            ], self.install),
            ServerAPI("uninstall_plugin", "Uninstall a plugin", [
                ArgParameter("plugin_url", "Plugin URL to uninstall", str),
            ], self.uninstall),
        ], service)

    def start(self):
        self.rpc_server.start()

    def stop(self):
        self.rpc_server.stop()

    def list_plugins(self):
        return [x.info() for x in self.plugin_manager.list()]

    def enable_plugin(self, plugin_url):
        db_plugin = self.get_plugin(plugin_url)
        db_plugin.enabled = True
        db_plugin.save()

        return self.plugin_manager.load_plugin(db_plugin).info()

    def disable_plugin(self, plugin_url):
        db_plugin = self.get_plugin(plugin_url)
        db_plugin.enabled = False
        db_plugin.save()

        return self.plugin_manager.load_plugin(db_plugin).info()

    def install(self, plugin_url):
        installed_plugin = self.plugin_manager.install(plugin_url)

        params = {
            "app_url": plugin_url,
            "name": installed_plugin.name,
            "machine": self.instance_data
        }
        if installed_plugin.description:
            params["description"] = installed_plugin.description

        plugin_data = PluginData(**params)
        plugin_data.save(force_insert=True)
        return installed_plugin.info()

    def uninstall(self, plugin_url):
        db_plugin = self.get_plugin(plugin_url)
        plugin = self.plugin_manager.uninstall(plugin_url)
        db_plugin.delete_instance()
        return plugin.info()

    def activate(self, plugin_url):
        db_plugin = self.get_plugin(plugin_url)
        token = self.registration_helper.register_plugin(db_plugin)
        return self.plugin_manager.activate(plugin_url, token).info()

    def deactivate(self, plugin_url):
        self.registration_helper.unregister_plugin(plugin_url)
        return self.plugin_manager.deactivate(plugin_url).info()

    def get_plugin(self, plugin_url):
        try:
            return PluginData.get(PluginData.app_url == plugin_url,
                                  PluginData.machine == self.instance_data)
        except DoesNotExist:
            raise ObjectNotFound(plugin_url)


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
        self.registration_helper = None

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
