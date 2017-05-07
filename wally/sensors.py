import bz2
import array
import logging
from typing import Dict

import numpy

from cephlib import sensors_rpc_plugin

from . import utils
from .test_run_class import TestRun
from .result_classes import DataSource
from .stage import Stage, StepOrder
from .hlstorage import ResultStorage


plugin_fname = sensors_rpc_plugin.__file__.rsplit(".", 1)[0] + ".py"
SENSORS_PLUGIN_CODE = open(plugin_fname, "rb").read()  # type: bytes


logger = logging.getLogger("wally")


# TODO(koder): in case if node has more than one role sensor settings might be incorrect
class StartSensorsStage(Stage):
    priority = StepOrder.START_SENSORS
    config_block = 'sensors'

    def run(self, ctx: TestRun) -> None:
        if  array.array('L').itemsize != 8:
            message = "Python array.array('L') items should be 8 bytes in size, not {}." + \
                " Can't provide sensors on this platform. Disable sensors in config and retry"
            logger.critical(message.format(array.array('L').itemsize))
            raise utils.StopTestError()

        # TODO: need carefully fix this
        # sensors config is:
        #   role:
        #     sensor: [str]
        # or
        #  role:
        #     sensor:
        #        allowed: [str]
        #        dissallowed: [str]
        #        params: Any
        per_role_config = {}  # type: Dict[str, Dict[str, str]]

        for name, val in ctx.config.sensors.roles_mapping.raw().items():
            if isinstance(val, str):
                val = {vl.strip(): (".*" if vl.strip() != 'ceph' else {}) for vl in val.split(",")}
            elif isinstance(val, list):
                val = {vl: (".*" if vl != 'ceph' else {}) for vl in val}
            per_role_config[name] = val

        if 'all' in per_role_config:
            all_vl = per_role_config.pop('all')
            all_roles = set(per_role_config)

            for node in ctx.nodes:
                all_roles.update(node.info.roles)  # type: ignore

            for name, vals in list(per_role_config.items()):
                new_vals = all_vl.copy()
                new_vals.update(vals)
                per_role_config[name] = new_vals

        for node in ctx.nodes:
            node_cfg = {}  # type: Dict[str, Dict[str, str]]
            for role in node.info.roles:
                node_cfg.update(per_role_config.get(role, {}))  # type: ignore

            nid = node.node_id
            if node_cfg:
                # ceph requires additional settings
                if 'ceph' in node_cfg:
                    node_cfg['ceph'].update(node.info.params['ceph'])
                    node_cfg['ceph']['osds'] = [osd['id'] for osd in node.info.params['ceph-osds']]  # type: ignore

                logger.debug("Setting up sensors RPC plugin for node %s", nid)
                node.upload_plugin("sensors", SENSORS_PLUGIN_CODE)
                ctx.sensors_run_on.add(nid)
                logger.debug("Start monitoring node %s", nid)
                node.conn.sensors.start(node_cfg)
            else:
                logger.debug("Skip monitoring node %s, as no sensors selected", nid)


def collect_sensors_data(ctx: TestRun, stop: bool = False):
    rstorage = ResultStorage(ctx.storage)
    total_sz = 0

    logger.info("Start loading sensors")
    for node in ctx.nodes:
        node_id = node.node_id
        if node_id in ctx.sensors_run_on:
            func = node.conn.sensors.stop if stop else node.conn.sensors.get_updates

            # hack to calculate total transferred size
            offset_map, compressed_blob, compressed_collected_at_b = func()
            data_tpl = (offset_map, compressed_blob, compressed_collected_at_b)

            total_sz += len(compressed_blob) + len(compressed_collected_at_b) + sum(map(len, offset_map)) + \
                16 * len(offset_map)

            for path, value, is_array, units in sensors_rpc_plugin.unpack_rpc_updates(data_tpl):
                if path == 'collected_at':
                    ds = DataSource(node_id=node_id, metric='collected_at', tag='csv')
                    rstorage.append_sensor(numpy.array(value), ds, units)
                else:
                    sensor, dev, metric = path.split(".")
                    ds = DataSource(node_id=node_id, metric=metric, dev=dev, sensor=sensor, tag='csv')
                    if is_array:
                        rstorage.append_sensor(numpy.array(value), ds, units)
                    else:
                        if metric == 'historic':
                            rstorage.put_sensor_raw(bz2.compress(value), ds(tag='bin'))
                        else:
                            assert metric in ('perf_dump', 'historic_js')
                            rstorage.put_sensor_raw(value, ds(tag='js'))
    logger.info("Download %sB of sensors data", utils.b2ssize(total_sz))



class CollectSensorsStage(Stage):
    priority = StepOrder.COLLECT_SENSORS
    config_block = 'sensors'

    def run(self, ctx: TestRun) -> None:
        collect_sensors_data(ctx, True)
