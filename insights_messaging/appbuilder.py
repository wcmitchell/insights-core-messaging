import logging
import logging.config
from logstash_formatter import LogstashFormatterV1
import os
import sys
import yaml

from insights import apply_configs, apply_default_enabled, dr

from .consumers.cli import Interactive
from .consumers import ArchiveContextIdsInjectingFilter
from .downloaders.localfs import LocalFS
from .engine import Engine
from .publishers.cli import StdOut
from .template import DefaultingTemplate as Template
from .watchers import ConsumerWatcher, EngineWatcher

Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def resolve_variables(v, env=os.environ):
    if isinstance(v, str):
        return Template(v).safe_substitute(env)

    if isinstance(v, dict):
        for k in v:
            v[k] = resolve_variables(v[k], env)
        return v

    if isinstance(v, list):
        return [resolve_variables(i, env) for i in v]

    return v


class AppBuilder:
    def __init__(self, manifest):
        if not isinstance(manifest, dict):
            manifest = yaml.load(manifest, Loader=Loader)

        manifest = resolve_variables(manifest)

        self.plugins = manifest.get("plugins", {})
        self.service = manifest.get("service", {})

    def _load_packages(self, pkgs):
        for p in pkgs:
            dr.load_components(p, continue_on_error=False)

    def _load_plugins(self):
        self._load_packages(self.plugins.get("packages", []))

    def _load(self, spec):
        comp = dr.get_component(spec["name"])
        if comp is None:
            raise Exception(f"Couldn't find {spec['name']}.")
        args = spec.get("args", [])
        kwargs = spec.get("kwargs", {})
        return comp(*args, **kwargs)

    def _get_consumer(self, publisher, downloader, engine, requeuer):
        if "consumer" not in self.service:
            return Interactive(publisher, downloader, engine)
        spec = self.service["consumer"]
        Consumer = dr.get_component(spec["name"])
        if Consumer is None:
            raise Exception(f"Couldn't find {spec['name']}.")
        args = spec.get("args", [])
        kwargs = spec.get("kwargs", {})
        return Consumer(publisher, downloader, engine, *args, requeuer=requeuer, **kwargs)

    def _get_requeuer(self):
        if "requeuer" in self.service:
            return self._load(self.service["requeuer"])

    def _get_publisher(self):
        if "publisher" not in self.service:
            return StdOut()
        spec = self.service["publisher"]
        return self._load(spec)

    def _get_graphs(self, target_components):
        graph = {}
        tc = tuple(target_components or [])
        if tc:
            for c in dr.DELEGATES:
                if dr.get_name(c).startswith(tc):
                    graph.update(dr.get_dependency_graph(c))
        return graph

    def _resolve_engine_config(self, config):
        return {
            "formatter": dr.get_component(config.get("format")),
            "target_components": self._get_graphs(config.get("target_components", [])),
            "extract_timeout": config.get("extract_timeout"),
            "extract_tmp_dir": config.get("extract_tmp_dir"),
        }

    def _get_engine(self):
        engine_config = self._resolve_engine_config(self.service)

        if "engine" not in self.service:
            return Engine(**engine_config)

        spec = self.service["engine"]
        kwargs = spec.get("kwargs", {})
        engine_config.update(self._resolve_engine_config(kwargs))

        EngineCls = dr.get_component(spec["name"])
        if EngineCls is None:
            raise Exception(f"Couldn't find {spec['name']}.")
        return EngineCls(**engine_config)

    # platform.payload-status
    def _get_downloader(self):
        if "downloader" not in self.service:
            return LocalFS
        return self._load(self.service["downloader"])

    def _get_watchers(self):
        if "watchers" not in self.service:
            return []
        return [self._load(w) for w in self.service["watchers"]]

    def _get_log_config(self):
        config = self.service.get("logging", {})
        configurator = self.service.get("logging_configurator")
        if configurator:
            c = self._load(configurator)
            config = c(config)
        return config

    def build_app(self):
        log_config = self._get_log_config()
        if log_config:
            logging.config.dictConfig(log_config)
        else:
            handler = logging.StreamHandler(stream=sys.stdout)
            handler.setFormatter(LogstashFormatterV1()) # to keep the same format with cloud env
            logging.basicConfig(level=logging.DEBUG)
            logging.getLogger("insights.core.dr").setLevel(logging.ERROR)
            # remove the default handler and just use the system stream handler for local environment
            logging.root.removeHandler(logging.root.handlers[0])
            logging.root.addHandler(handler)

        # add filter for handlers in root logger
        payload_inject_filter = ArchiveContextIdsInjectingFilter()
        for i_handler in logging.root.handlers:
            i_handler.addFilter(payload_inject_filter)
        self._load_plugins()
        apply_default_enabled(self.plugins)
        apply_configs(self.plugins)

        downloader = self._get_downloader()
        engine = self._get_engine()
        publisher = self._get_publisher()
        requeuer = self._get_requeuer()
        consumer = self._get_consumer(publisher, downloader, engine, requeuer)

        for w in self._get_watchers():
            if isinstance(w, EngineWatcher):
                w.watch(engine)
            if isinstance(w, ConsumerWatcher):
                w.watch(consumer)

        return consumer
