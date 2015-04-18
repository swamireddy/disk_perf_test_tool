from __future__ import print_function

import os
import sys
import Queue
import pprint
import logging
import argparse
import functools
import threading
import contextlib
import collections

import yaml
from concurrent.futures import ThreadPoolExecutor

from wally import pretty_yaml
from wally.tests_sensors import deploy_sensors_stage
from wally.discover import discover, Node, undiscover
from wally import utils, report, ssh_utils, start_vms
from wally.suits.itest import IOPerfTest, PgBenchTest
from wally.config import cfg_dict, load_config, setup_loggers


logger = logging.getLogger("wally")


def format_result(res, formatter):
    data = "\n{0}\n".format("=" * 80)
    data += pprint.pformat(res) + "\n"
    data += "{0}\n".format("=" * 80)
    templ = "{0}\n\n====> {1}\n\n{2}\n\n"
    return templ.format(data, formatter(res), "=" * 80)


class Context(object):
    def __init__(self):
        self.build_meta = {}
        self.nodes = []
        self.clear_calls_stack = []
        self.openstack_nodes_ids = []
        self.sensors_mon_q = None


def connect_one(node, vm=False):
    try:
        ssh_pref = "ssh://"
        if node.conn_url.startswith(ssh_pref):
            url = node.conn_url[len(ssh_pref):]

            if vm:
                ret_count = 24
                log_warns = False
            else:
                ret_count = 3
                log_warns = True

            node.connection = ssh_utils.connect(url,
                                                retry_count=ret_count,
                                                log_warns=log_warns)
        else:
            raise ValueError("Unknown url type {0}".format(node.conn_url))
    except Exception:
        logger.exception("During connect to " + node.get_conn_id())
        node.connection = None


def connect_all(nodes, vm=False):
    logger.info("Connecting to nodes")
    with ThreadPoolExecutor(32) as pool:
        connect_one_f = functools.partial(connect_one, vm=vm)
        list(pool.map(connect_one_f, nodes))


def test_thread(test, node, barrier, res_q):
    try:
        logger.debug("Run preparation for {0}".format(node.conn_url))
        test.pre_run(node.connection)
        logger.debug("Run test for {0}".format(node.conn_url))
        test.run(node.connection, barrier)
    except Exception as exc:
        logger.exception("In test {0} for node {1}".format(test, node))
        res_q.put(exc)

    try:
        test.cleanup(node.connection)
    except:
        msg = "Duringf cleanup - in test {0} for node {1}"
        logger.exception(msg.format(test, node))


def run_tests(test_block, nodes):
    tool_type_mapper = {
        "io": IOPerfTest,
        "pgbench": PgBenchTest,
    }

    test_nodes = [node for node in nodes
                  if 'testnode' in node.roles]
    test_number_per_type = {}
    res_q = Queue.Queue()

    for name, params in test_block.items():
        logger.info("Starting {0} tests".format(name))
        test_num = test_number_per_type.get(name, 0)
        test_number_per_type[name] = test_num + 1
        threads = []
        barrier = utils.Barrier(len(test_nodes))

        for node in test_nodes:
            msg = "Starting {0} test on {1} node"
            logger.debug(msg.format(name, node.conn_url))

            dr = os.path.join(
                    cfg_dict['test_log_directory'],
                    "{0}_{1}_{2}".format(name, test_num, node.get_ip())
                )

            if not os.path.exists(dr):
                os.makedirs(dr)

            test = tool_type_mapper[name](params, res_q.put, dr,
                                          node=node.get_ip())
            th = threading.Thread(None, test_thread, None,
                                  (test, node, barrier, res_q))
            threads.append(th)
            th.daemon = True
            th.start()

        def gather_results(res_q, results):
            while not res_q.empty():
                val = res_q.get()

                if isinstance(val, Exception):
                    msg = "Exception during test execution: {0}"
                    raise ValueError(msg.format(val.message))

                results.append(val)

        results = []

        while True:
            for th in threads:
                th.join(1)
                gather_results(res_q, results)

            if all(not th.is_alive() for th in threads):
                break

        gather_results(res_q, results)
        yield name, test.merge_results(results)


def log_nodes_statistic(_, ctx):
    nodes = ctx.nodes
    logger.info("Found {0} nodes total".format(len(nodes)))
    per_role = collections.defaultdict(lambda: 0)
    for node in nodes:
        for role in node.roles:
            per_role[role] += 1

    for role, count in sorted(per_role.items()):
        logger.debug("Found {0} nodes with role {1}".format(count, role))


def connect_stage(cfg, ctx):
    ctx.clear_calls_stack.append(disconnect_stage)
    connect_all(ctx.nodes)

    all_ok = True

    for node in ctx.nodes:
        if node.connection is None:
            if 'testnode' in node.roles:
                msg = "Can't connect to testnode {0}"
                raise RuntimeError(msg.format(node.get_conn_id()))
            else:
                msg = "Node {0} would be excluded - can't connect"
                logger.warning(msg.format(node.get_conn_id()))
                all_ok = False

    if all_ok:
        logger.info("All nodes connected successfully")

    ctx.nodes = [node for node in ctx.nodes
                 if node.connection is not None]


def discover_stage(cfg, ctx):
    if cfg.get('discover') is not None:
        discover_objs = [i.strip() for i in cfg['discover'].strip().split(",")]

        nodes, clean_data = discover(ctx, discover_objs,
                                     cfg['clouds'], cfg['var_dir'])

        def undiscover_stage(cfg, ctx):
            undiscover(clean_data)

        ctx.clear_calls_stack.append(undiscover_stage)
        ctx.nodes.extend(nodes)

    for url, roles in cfg.get('explicit_nodes', {}).items():
        ctx.nodes.append(Node(url, roles.split(",")))


def get_os_credentials(cfg, ctx, creds_type):
    creds = None

    if creds_type == 'clouds':
        if 'openstack' in cfg['clouds']:
            os_cfg = cfg['clouds']['openstack']

            tenant = os_cfg['OS_TENANT_NAME'].strip()
            user = os_cfg['OS_USERNAME'].strip()
            passwd = os_cfg['OS_PASSWORD'].strip()
            auth_url = os_cfg['OS_AUTH_URL'].strip()

        elif 'fuel' in cfg['clouds'] and \
             'openstack_env' in cfg['clouds']['fuel']:
            creds = ctx.fuel_openstack_creds

    elif creds_type == 'ENV':
        user, passwd, tenant, auth_url = start_vms.ostack_get_creds()
    elif os.path.isfile(creds_type):
        raise NotImplementedError()
        # user, passwd, tenant, auth_url = start_vms.ostack_get_creds()
    else:
        msg = "Creds {0!r} isn't supported".format(creds_type)
        raise ValueError(msg)

    if creds is None:
        creds = {'name': user,
                 'passwd': passwd,
                 'tenant': tenant,
                 'auth_url': auth_url}

    return creds


@contextlib.contextmanager
def create_vms_ctx(ctx, cfg, config):
    params = config['vm_params'].copy()
    os_nodes_ids = []

    os_creds_type = config['creds']
    os_creds = get_os_credentials(cfg, ctx, os_creds_type)

    start_vms.nova_connect(**os_creds)

    logger.info("Preparing openstack")
    start_vms.prepare_os_subpr(**os_creds)

    new_nodes = []
    try:
        params['group_name'] = cfg_dict['run_uuid']
        for new_node, node_id in start_vms.launch_vms(params):
            new_node.roles.append('testnode')
            ctx.nodes.append(new_node)
            os_nodes_ids.append(node_id)
            new_nodes.append(new_node)

        store_nodes_in_log(cfg, os_nodes_ids)
        ctx.openstack_nodes_ids = os_nodes_ids

        yield new_nodes

    finally:
        if not cfg['keep_vm']:
            shut_down_vms_stage(cfg, ctx)


def run_tests_stage(cfg, ctx):
    ctx.results = []

    if 'tests' not in cfg:
        return

    for group in cfg['tests']:

        assert len(group.items()) == 1
        key, config = group.items()[0]

        if 'start_test_nodes' == key:
            with create_vms_ctx(ctx, cfg, config) as new_nodes:
                connect_all(new_nodes, True)

                for node in new_nodes:
                    if node.connection is None:
                        msg = "Failed to connect to vm {0}"
                        raise RuntimeError(msg.format(node.get_conn_id()))

                deploy_sensors_stage(cfg_dict,
                                     ctx,
                                     nodes=new_nodes,
                                     undeploy=False)

                for test_group in config.get('tests', []):
                    ctx.results.extend(run_tests(test_group, ctx.nodes))
        else:
            ctx.results.extend(run_tests(group, ctx.nodes))


def shut_down_vms_stage(cfg, ctx):
    vm_ids_fname = cfg_dict['vm_ids_fname']
    if ctx.openstack_nodes_ids is None:
        nodes_ids = open(vm_ids_fname).read().split()
    else:
        nodes_ids = ctx.openstack_nodes_ids

    if len(nodes_ids) != 0:
        logger.info("Removing nodes")
        start_vms.clear_nodes(nodes_ids)
        logger.info("Nodes has been removed")

    if os.path.exists(vm_ids_fname):
        os.remove(vm_ids_fname)


def store_nodes_in_log(cfg, nodes_ids):
    with open(cfg['vm_ids_fname'], 'w') as fd:
        fd.write("\n".join(nodes_ids))


def clear_enviroment(cfg, ctx):
    if os.path.exists(cfg_dict['vm_ids_fname']):
        shut_down_vms_stage(cfg, ctx)


def disconnect_stage(cfg, ctx):
    ssh_utils.close_all_sessions()

    for node in ctx.nodes:
        if node.connection is not None:
            node.connection.close()


def store_raw_results_stage(cfg, ctx):

    raw_results = os.path.join(cfg_dict['var_dir'], 'raw_results.yaml')

    if os.path.exists(raw_results):
        cont = yaml.load(open(raw_results).read())
    else:
        cont = []

    cont.extend(utils.yamable(ctx.results))
    raw_data = pretty_yaml.dumps(cont)

    with open(raw_results, "w") as fd:
        fd.write(raw_data)


def console_report_stage(cfg, ctx):
    for tp, data in ctx.results:
        if 'io' == tp and data is not None:
            print(IOPerfTest.format_for_console(data))


def report_stage(cfg, ctx):
    html_rep_fname = cfg['html_report_file']

    try:
        fuel_url = cfg['clouds']['fuel']['url']
    except KeyError:
        fuel_url = None

    try:
        creds = cfg['clouds']['fuel']['creds']
    except KeyError:
        creds = None

    report.make_io_report(ctx.results, html_rep_fname, fuel_url, creds=creds)

    logger.info("Html report were stored in " + html_rep_fname)

    text_rep_fname = cfg_dict['text_report_file']
    with open(text_rep_fname, "w") as fd:
        for tp, data in ctx.results:
            if 'io' == tp and data is not None:
                fd.write(IOPerfTest.format_for_console(data))
                fd.write("\n")
                fd.flush()

    logger.info("Text report were stored in " + text_rep_fname)


def complete_log_nodes_statistic(cfg, ctx):
    nodes = ctx.nodes
    for node in nodes:
        logger.debug(str(node))


def load_data_from(var_dir):
    def load_data_from_file(cfg, ctx):
        raw_results = os.path.join(var_dir, 'raw_results.yaml')
        ctx.results = yaml.load(open(raw_results).read())
    return load_data_from_file


def parse_args(argv):
    descr = "Disk io performance test suite"
    parser = argparse.ArgumentParser(prog='wally', description=descr)

    parser.add_argument("-l", dest='extra_logs',
                        action='store_true', default=False,
                        help="print some extra log info")
    parser.add_argument("-b", '--build_description',
                        type=str, default="Build info")
    parser.add_argument("-i", '--build_id', type=str, default="id")
    parser.add_argument("-t", '--build_type', type=str, default="GA")
    parser.add_argument("-u", '--username', type=str, default="admin")
    parser.add_argument("-p", '--post-process-only', metavar="VAR_DIR",
                        help="Only process data from previour run")
    parser.add_argument("-k", '--keep-vm', action='store_true',
                        help="Don't remove test vm's", default=False)
    parser.add_argument("config_file")

    return parser.parse_args(argv[1:])


def main(argv):
    opts = parse_args(argv)

    if opts.post_process_only is not None:
        stages = [
            load_data_from(opts.post_process_only),
            console_report_stage,
            report_stage
        ]
    else:
        stages = [
            discover_stage,
            log_nodes_statistic,
            connect_stage,
            deploy_sensors_stage,
            run_tests_stage,
            store_raw_results_stage,
            console_report_stage,
            report_stage
        ]

    load_config(opts.config_file, opts.post_process_only)

    if cfg_dict.get('logging', {}).get("extra_logs", False) or opts.extra_logs:
        level = logging.DEBUG
    else:
        level = logging.WARNING

    setup_loggers(level, cfg_dict['log_file'])

    logger.info("All info would be stored into {0}".format(
        cfg_dict['var_dir']))

    ctx = Context()
    ctx.build_meta['build_id'] = opts.build_id
    ctx.build_meta['build_descrption'] = opts.build_description
    ctx.build_meta['build_type'] = opts.build_type
    ctx.build_meta['username'] = opts.username
    cfg_dict['keep_vm'] = opts.keep_vm

    try:
        for stage in stages:
            logger.info("Start {0.__name__} stage".format(stage))
            stage(cfg_dict, ctx)
    except Exception as exc:
        msg = "Exception during current stage: {0}".format(exc.message)
        logger.error(msg)
    finally:
        exc, cls, tb = sys.exc_info()
        for stage in ctx.clear_calls_stack[::-1]:
            try:
                logger.info("Start {0.__name__} stage".format(stage))
                stage(cfg_dict, ctx)
            except Exception as exc:
                logger.exception("During {0.__name__} stage".format(stage))

        if exc is not None:
            raise exc, cls, tb

    logger.info("All info stored into {0}".format(cfg_dict['var_dir']))
    return 0