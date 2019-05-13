import json
import os
import shutil
import subprocess
import textwrap

import pytest

from weavelib.exceptions import PluginLoadError
from weavelib.services import BasePlugin

from weaveenv.plugins import load_plugin_json, VirtualEnvManager


class TestPluginLoadJson(object):
    @pytest.fixture(autouse=True)
    def setup(self, tmpdir):
        self.tmpdir = tmpdir.strpath
        self.plugin_dir = os.path.join(self.tmpdir, "plugin")
        os.makedirs(self.plugin_dir)

    def write_content(self, path, contents):
        with open(os.path.join(self.plugin_dir, path), "w") as out:
            out.write(contents)

    def test_no_plugin_json(self):
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Error opening plugin.json."

    def test_invalid_json(self):
        self.write_content("plugin.json", "hello word.")

        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Error parsing plugin.json."

    def test_no_service_field(self):
        self.write_content("plugin.json", '{"hello": "world"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Required field not found in plugin.json."

    def test_bad_service_specification(self):
        self.write_content("plugin.json", '{"service": "hello world"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Bad 'service' specification in plugin.json."

    def test_failing_import(self):
        self.write_content("plugin.json", '{"service": "a.b.c"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Failed to import dependencies."

    def test_not_base_plugin(self):
        # This just tries to import from weaveenv
        self.write_content("plugin.json",
                           '{"service": "weaveenv.plugins.VirtualEnvManager"}')
        with pytest.raises(PluginLoadError) as ex:
            load_plugin_json(self.plugin_dir)

        assert ex.value.extra == "Service must inherit BasePlugin."

    def test_good_plugin_load(self):
        spec = {
            "service": "plugin.main.TestPlugin",
            "config": {"hello": "world"},
            "start_timeout": 10,
            "deps": [1, 2, 4]
        }
        main_py = """
        from weavelib.services import BasePlugin

        class TestPlugin(BasePlugin):
            pass
        """
        package_dir = os.path.join(self.plugin_dir, "plugin")
        main_py_path = os.path.join(self.plugin_dir, "plugin", "main.py")

        os.makedirs(self.plugin_dir + "/plugin")
        self.write_content("plugin.json", json.dumps(spec))
        self.write_content(main_py_path, textwrap.dedent(main_py))

        expected = {
            "deps": [1, 2, 4],
            "package_path": "plugin.main.TestPlugin",
            "config": {"hello": "world"},
            "start_timeout": 10
        }

        plugin_info = load_plugin_json(self.plugin_dir)
        plugin_class = plugin_info.pop("service_cls")

        assert expected == plugin_info
        assert plugin_class.__name__ == "TestPlugin"


class TestVirtualEnvManager(object):
    @pytest.fixture(autouse=True)
    def setup(self, tmpdir):
        self.tmpdir = tmpdir.strpath
        self.venv_dir = os.path.join(self.tmpdir, "venv")
        self.tmp_dir = os.path.join(self.tmpdir, "scratch")
        os.makedirs(self.tmp_dir)

    def teardown(self):
        shutil.rmtree(self.tmpdir)

    def write_content(self, path, contents):
        with open(os.path.join(self.tmp_dir, path), "w") as out:
            out.write(contents)

    def test_create_virtual_env(self):
        venv = VirtualEnvManager(self.venv_dir)

        assert venv.install()
        assert venv.install()

    def test_create_virtual_env_with_bad_requirements(self):
        self.write_content("requirements.txt", "skdjdlss_slkdjs")

        venv = VirtualEnvManager(self.venv_dir)

        assert not venv.install(os.path.join(self.tmp_dir, "requirements.txt"))

    def test_create_virtual_env_with_requirements(self):
        self.write_content("requirements.txt", "bottle")

        venv = VirtualEnvManager(self.venv_dir)

        assert venv.install(os.path.join(self.tmp_dir, "requirements.txt"))

        python = os.path.join(self.venv_dir, "bin/python")
        command = [python, "-c", "import bottle"]
        subprocess.check_call(command)

        assert venv.is_installed()

        venv.clean()

        assert not venv.is_installed()
