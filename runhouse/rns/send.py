import inspect
import logging
import requests
import json
from typing import Optional, Callable, Union, List, Tuple
import os
from pathlib import Path
import ray.cloudpickle as pickle

from runhouse.rns.resource import Resource
from runhouse.rns.hardware import Cluster
from runhouse.rns.package import Package
from runhouse.rns.api_utils.utils import read_response_data, is_jsonable
from runhouse.rns.api_utils.resource_access import ResourceAccess
from runhouse import rh_config

logger = logging.getLogger(__name__)


class Send(Resource):
    RESOURCE_TYPE = 'send'
    DEFAULT_HARDWARE = '^rh-cpu'
    DEFAULT_ACCESS = 'write'

    def __init__(self,
                 fn_pointers: Tuple[str, str, str],
                 hardware: Optional[Cluster] = None,
                 name: [Optional[str]] = None,
                 dedicated: Optional[bool] = False,
                 reqs: Optional[List[str]] = None,
                 image: Optional[str] = None,  # TODO
                 save_to: Optional[List[str]] = None,
                 dryrun: Optional[bool] = True,
                 access: Optional[str] = None,
                 **kwargs  # We have this here to ignore extra arguments when calling from from_config
                 ):
        """
        Create, load, or update a Send ("Serverless endpoint"). A Send is comprised of the package,
        entrypoint, hardware, and dependencies (requirements or image) to run the service.

        Args:
            fn (): A python callable or entrypoint string (module:function) within the package which the user
                can call to run remotely as a microservice. The Send object is callable, taking the same inputs
                 and producing the same outputs as fn. For example, if we create
                 `my_send = Send(fn=lambda x: x+1, hardware=my_hw)`, we can call it with `my_send(4)`, and
                 the fn will run remotely on `my_hw`.
            name (): A URI to persist this Send's metadata to Runhouse's Resource Naming System (RNS), which allows
                it to be reloaded and used later or in other environments. If name is left empty, the Send is
                "anonymous" and will only exist within this Python context. # TODO more about user namespaces.
            hardware ():
            parent ():
            reqs ():
            cluster ():
        """
        # TODO add function setter for better interactivity in notebooks
        self.fn_pointers = fn_pointers
        self.hardware = hardware
        if reqs is None:
            reqs = [f'reqs:{rh_config.rns_client.locate_working_dir()}']
        self.reqs = reqs
        self.image = image  # TODO or self.DEFAULT_IMAGE
        self.access = access or self.DEFAULT_ACCESS

        # if we aren't in setup mode we are presumably calling an existing Send on an existing cluster
        # ex: Consuming the Send as a reader after the creator has shared it with you
        # TODO maybe infer setup mode by looking at fields provided
        #  ex: bool(name and not reqs and not hardware and not cluster and not folder and not fn)
        self.dryrun = dryrun
        super().__init__(name=name, dryrun=dryrun, save_to=save_to)

        # TODO dedicated vs. shared mode for hardware

        if not self.dryrun and self.access in ['write', 'read']:
            if not self.hardware.is_up():
                self.reup_cluster()
            logging.info('Setting up Send on cluster.')
            self.hardware.install_packages(self.reqs)
            logging.info('Send setup complete.')

    # ----------------- Constructor helper methods -----------------

    @staticmethod
    def from_config(config, dryrun=True):
        """Create a Send object from a config dictionary.

        Args:
            config (dict): Dictionary of config values.

        Returns:
            Send: Send object created from config values.
        """
        config['reqs'] = [Package.from_config(package, dryrun=True)
                          if isinstance(package, dict) else package
                          for package in config['reqs']]
        # TODO validate which fields need to be present in the config

        if isinstance(config['hardware'], dict):
            config['hardware'] = Cluster.from_config(config['hardware'], dryrun=dryrun)

        if 'fn_pointers' not in config:
            raise ValueError("No fn_pointers provided in config. Please provide a path "
                             "to a python file, module, and function name.")

        return Send(**config, dryrun=dryrun)

    def reup_cluster(self):
        logger.info(f"Upping the cluster {self.hardware.name}")
        # Even if cluster is already up, copies working_dir onto the cluster inside of image
        self.hardware.up()

    @staticmethod
    def extract_fn_paths(raw_fn, reqs):
        """Get the path to the module, module name, and function name to be able to import it on the server"""
        if not isinstance(raw_fn, Callable):
            raise TypeError(f"Invalid fn for Send, expected Callable but received {type(raw_fn)}")
        # Background on all these dunders: https://docs.python.org/3/reference/import.html
        module = inspect.getmodule(raw_fn)

        # Need to resolve in case just filename is given
        module_path = str(Path(inspect.getfile(module)).resolve()) if hasattr(module, '__file__') else None

        if not module_path or raw_fn.__name__ == "<lambda>":
            # The only time __file__ wouldn't be present is if the function is defined in an interactive
            # interpreter or a notebook. We can't import on the server in that case, so we need to cloudpickle
            # the fn to send it over. The __call__ function will serialize the function if we return it this way.
            # This is a short-term hack.
            # return None, "notebook", raw_fn.__name__
            root_path = os.getcwd()
            module_name = 'notebook'
            fn_name = raw_fn.__name__
        else:
            root_path = os.path.dirname(module_path)
            # module_name = getattr(module.__spec__, 'name', inspect.getmodulename(module_path))
            module_name = module.__spec__.name if getattr(module, "__package__", False) \
                else inspect.getmodulename(module_path)
            # TODO __qualname__ doesn't work when fn is aliased funnily, like torch.sum
            fn_name = getattr(raw_fn, '__qualname__', raw_fn.__name__)

        # if module is not in a package, we need to add its parent directory to the path to import it
        # if not getattr(module, '__package__', None):
        #     module_path = os.path.dirname(module.__file__)

        remote_import_path = None
        for req in reqs:
            local_path = None
            if not isinstance(req, str) and req.is_local():
                local_path = Path(req.local_path)
            elif isinstance(req, str):
                if req.split(':')[0] in ['local', 'reqs']:
                    req = req.split(':')[1]

                if Path(req).expanduser().resolve().exists():
                    # Relative paths are relative to the working directory in Folders/Packages!
                    local_path = Path(req).expanduser() if Path(req).expanduser().is_absolute() \
                        else Path(rh_config.rns_client.locate_working_dir()) / req

            if local_path:
                try:
                    # Module path relative to package
                    remote_import_path = local_path.name + '/' + str(Path(root_path).relative_to(local_path))
                except ValueError:  # Not a subdirectory
                    pass
        return remote_import_path, module_name, fn_name

    # Found in python decorator logic, maybe use
    # func_name = getattr(f, '__qualname__', f.__name__)
    # module_name = getattr(f, '__module__', '')
    # if module_name:
    #     full_name = f'{module_name}.{func_name}'
    # else:
    #     full_name = func_name

    # ----------------- Send call methods -----------------

    def __call__(self, *args, **kwargs):
        if self.access in [ResourceAccess.write, ResourceAccess.read]:
            return self._call_fn_with_ssh_access(fn_type='call', args=args, kwargs=kwargs)
        else:
            # run the function via http url - user only needs Proxy access
            if self.access != ResourceAccess.proxy:
                raise RuntimeError("Running http url requires proxy access")
            if not rh_config.rns_client.token:
                raise ValueError("Token must be saved in the local .rh config in order to use an http url")
            http_url = self.http_url()
            logger.info(f"Running {self.name} via http url: {http_url}")
            resp = requests.post(http_url,
                                 data=json.dumps({"args": args, "kwargs": kwargs}),
                                 headers=rh_config.rns_client.request_headers)
            if resp.status_code != 200:
                raise Exception(f'Failed to run Send endpoint: {json.loads(resp.content)}')

            res = read_response_data(resp)
            return res

    def repeat(self, num_repeats, *args, **kwargs):
        """Repeat the Send call multiple times.

        Args:
            num_repeats (int): Number of times to repeat the Send call.
            *args: Positional arguments to pass to the Send
            **kwargs: Keyword arguments to pass to the Send
        """
        if self.access in [ResourceAccess.write, ResourceAccess.read]:
            return self._call_fn_with_ssh_access(fn_type='repeat', args=[num_repeats, args], kwargs=kwargs)
        else:
            raise NotImplementedError("Send.repeat only works with Write or Read access, not Proxy access")

    def map(self, arg_list, **kwargs):
        """Map a function over a list of arguments."""
        if self.access in [ResourceAccess.write, ResourceAccess.read]:
            return self._call_fn_with_ssh_access(fn_type='map', args=arg_list, kwargs=kwargs)
        else:
            raise NotImplementedError("Send.map only works with Write or Read access, not Proxy access")

    def starmap(self, args_lists, **kwargs):
        """ Like Send.map() except that the elements of the iterable are expected to be iterables
        that are unpacked as arguments. An iterable of [(1,2), (3, 4)] results in [func(1,2), func(3,4)]."""
        if self.access in [ResourceAccess.write, ResourceAccess.read]:
            return self._call_fn_with_ssh_access(fn_type='starmap', args=args_lists, kwargs=kwargs)
        else:
            raise NotImplementedError("Send.starmap only works with Write or Read access, not Proxy access")

    def enqueue(self, *args, **kwargs):
        """Enqueue a Send call to be run later.

        Args:
            *args: Positional arguments to pass to the Send
            **kwargs: Keyword arguments to pass to the Send
        """
        if self.access in [ResourceAccess.write, ResourceAccess.read]:
            return self._call_fn_with_ssh_access(fn_type='queue', args=args, kwargs=kwargs)
        else:
            raise NotImplementedError("Send.enqueue only works with Write or Read access, not Proxy access")

    def remote(self, *args, **kwargs):
        """Map a function over a list of arguments."""
        # We need to ray init here so the returned Ray object ref doesn't throw an error it's deserialized
        import ray
        ray.init(ignore_reinit_error=True)
        if self.access in [ResourceAccess.write, ResourceAccess.read]:
            return self._call_fn_with_ssh_access(fn_type='remote', args=args, kwargs=kwargs)
        else:
            raise NotImplementedError("Send.map only works with Write or Read access, not Proxy access")

    def get(self, obj_ref):
        """Get the result of a Send call that was submitted as async using `remote`.

        Args:
            obj_ref: A single or list of Ray.ObjectRef objects returned by a Send.remote() call. The ObjectRefs
                must be from the cluster that this Send is running on.
        """
        if self.access in [ResourceAccess.write, ResourceAccess.read]:
            arg_list = obj_ref if isinstance(obj_ref, list) else [obj_ref]
            return self._call_fn_with_ssh_access(fn_type='get', args=arg_list, kwargs={})
        else:
            raise NotImplementedError("Send.get only works with Write or Read access, not Proxy access")

    def _call_fn_with_ssh_access(self, fn_type, args, kwargs):
        # https://docs.ray.io/en/latest/ray-core/tasks/patterns/map-reduce.html
        # return ray.get([map.remote(i, map_func) for i in replicas])
        # TODO allow specifying resources per worker for map
        name = self.name or 'anonymous send'
        logger.info(f"Running {name} via SSH")
        if self.fn_pointers is None:
            raise RuntimeError(f"No fn pointers saved for {name}")

        [relative_path, module_name, fn_name] = self.fn_pointers
        serialized_func: bytes = pickle.dumps([relative_path, module_name, fn_name, fn_type, args, kwargs])

        raw_resp = self.hardware.call_grpc(serialized_func=serialized_func)
        raw_msg: bytes = raw_resp.message
        [res, fn_exception, fn_traceback] = pickle.loads(raw_msg)
        if fn_exception is not None:
            logger.error(f"Error inside send {fn_type}: {fn_exception}.")
            logger.error(f"Traceback: {fn_traceback}")
            raise fn_exception
        return res

    # TODO [DG] test this properly
    def debug(self, redirect_logging=False, timeout=10000, *args, **kwargs):
        """ Run the Send in debug mode. This will run the Send through a tunnel interpreter, which
        allows the use of breakpoints and other debugging tools, like rh.ipython().
        FYI, alternative ideas from Ray: https://github.com/ray-project/ray/issues/17197
        FYI, alternative Modal folks shared: https://github.com/modal-labs/modal-client/pull/32
        """
        # Importing this here because they're heavy
        from plumbum.machines.paramiko_machine import ParamikoMachine
        from paramiko import AutoAddPolicy
        from rpyc.utils.zerodeploy import DeployedServer
        from rpyc.utils.classic import redirected_stdio

        creds = self.hardware.ssh_creds()
        ssh_client = ParamikoMachine(self.hardware.address,
                                     user=creds['ssh_user'],
                                     keyfile=str(Path(creds['ssh_private_key']).expanduser()),
                                     missing_host_policy=AutoAddPolicy())
        server = DeployedServer(ssh_client, server_class='rpyc.utils.server.ForkingServer')
        conn = server.classic_connect()

        if redirect_logging:
            rlogger = conn.modules.logging.getLogger()
            rlogger.parent = logging.getLogger()

        conn._config["sync_request_timeout"] = timeout  # seconds. May need to be longer for real debugging.
        conn._config["allow_public_attrs"] = True
        conn._config["allow_pickle"] = True
        # This assumes the code is already synced over to the remote container
        remote_fn = getattr(conn.modules[self.fn.__module__], self.fn.__name__)

        with redirected_stdio(conn):
            res = remote_fn(*args, **kwargs)
        conn.close()
        return res

    @property
    def config_for_rns(self):
        # TODO save Package resource, because fn_pointers are meaningless without the package.

        config = super().config_for_rns

        config.update({
            'hardware': self._resource_string_for_subconfig(self.hardware),
            'reqs': [self._resource_string_for_subconfig(package) for package in self.reqs],
            'fn_pointers': self.fn_pointers,
        })
        return config

    # TODO maybe reuse these if we starting putting each send in its own container
    # @staticmethod
    # def run_ssh_cmd_in_cluster(ssh_key, ssh_user, address, cmd, port_fwd=None):
    #     subprocess.run("ssh -tt -o IdentitiesOnly=yes -i "
    #                    f"{ssh_key} {port_fwd or ''}"
    #                    f"{ssh_user}@{address} docker exec -it ray_container /bin/bash -c {cmd}".split(' '))

    def ssh(self):
        if self.hardware is None:
            raise RuntimeError("Hardware must be specified and up to ssh into a Send")
        self.hardware.ssh()

    def send_secrets(self, reload=False):
        self.hardware.send_secrets(reload=reload)

    def http_url(self, curl_command=False, *args, **kwargs) -> str:
        """Return the endpoint needed to run the Send on the remote cluster, or provide the curl command if requested"""
        resource_uri = rh_config.rns_client.resource_uri(name=self.name)
        uri = f'proxy/{resource_uri}'
        if curl_command:
            # NOTE: curl command should include args and kwargs - this will help us generate better API docs
            if not is_jsonable(args) or not is_jsonable(kwargs):
                raise Exception('Invalid Send func params provided, must be able to convert args and kwargs to json')

            return "curl -X 'POST' '{api_server_url}/proxy{resource_uri}/endpoint' " \
                   "-H 'accept: application/json' " \
                   "-H 'Authorization: Bearer {auth_token}' " \
                   "-H 'Content-Type: application/json' " \
                   "-d '{data}'".format(api_server_url=rh_config.rns_client.api_server_url,
                                        resource_uri=uri,
                                        auth_token=rh_config.rns_client.token,
                                        data=json.dumps({"args": args, "kwargs": kwargs}))

        # HTTP URL needed to run the Send remotely
        http_url = f'{rh_config.rns_client.api_server_url}/{uri}/endpoint'
        return http_url

    def notebook(self, persist=False, sync_package_on_close=None):
        # Roughly trying to follow:
        # https://towardsdatascience.com/using-jupyter-notebook-running-on-a-remote-docker-container-via-ssh-ea2c3ebb9055
        # https://docs.ray.io/en/latest/ray-core/using-ray-with-jupyter.html
        if self.hardware is None:
            raise RuntimeError("Cannot SSH, running locally")

        tunnel, port_fwd = self.hardware.ssh_tunnel(local_port=8888, num_ports_to_try=10)
        try:
            install_cmd = "pip install jupyterlab"
            jupyter_cmd = f'jupyter lab --port {port_fwd} --no-browser'
            # port_fwd = '-L localhost:8888:localhost:8888 '  # TOOD may need when we add docker support
            with self.hardware.pause_autostop():
                self.hardware.run(commands=[install_cmd, jupyter_cmd], stream_logs=True)

        finally:
            if sync_package_on_close:
                if sync_package_on_close == 'default':
                    sync_package_on_close = rh_config.rns_client.locate_working_dir()
                pkg = Package.from_string('local:' + sync_package_on_close)
                self.hardware.rsync(source=f'~/{pkg.name}', dest=pkg.local_path, up=False)
            if not persist:
                tunnel.stop(force=True)
                kill_jupyter_cmd = f'jupyter notebook stop {port_fwd}'
                self.hardware.run(commands=[kill_jupyter_cmd])

    # TODO
    def keep_warm(self,
                  autostop_mins=None,
                  regions: List[str] = None,
                  min_replicas: List[int] = None,
                  max_replicas: List[int] = None):
        # TODO: For now just upping the cluster with the autostop provided
        if autostop_mins is None:
            logger.info(f"Keeping {self.name} indefinitely warm")
            # keep indefinitely warm if user doesn't specify
            autostop_mins = -1
        self.hardware.keep_warm(autostop_mins=autostop_mins)


def send(fn: Optional[Union[str, Callable]] = None,
         name: [Optional[str]] = None,
         hardware: Optional[Union[str, Cluster]] = None,
         reqs: Optional[List[str]] = None,
         image: Optional[str] = None,  # TODO
         load_from: Optional[List[str]] = None,
         save_to: Optional[List[str]] = None,
         dryrun: Optional[bool] = False,
         load_secrets: Optional[bool] = False,
         serialize_notebook_fn: Optional[bool] = False,
         ):
    """ Factory constructor to construct the Send for various provider types.

        fn: The function which will execute on the remote cluster when this send is called.
        name: Name of the Send to create or retrieve, either from a local config or from the RNS.
        hardware: Hardware to use for the Send, either a string name of a Cluster object, or a Cluster object.
        package: Package to send to the remote cluster, either a string name of a Package, package url,
            or a Package object.
        reqs: List of requirements to install on the remote cluster, or path to a requirements.txt file. If a list
            of pypi packages is provided, including 'requirements.txt' in the list will install the requirements
            in `package`. By default, if reqs is left as None, we'll set it to ['requirements.txt'], which installs
            just the requirements of package. If an empty list is provided, no requirements will be installed.
        image (TODO): Docker image id to use on the remote cluster, or path to Dockerfile.
        dryrun: Whether to create the Send if it doesn't exist, or load the Send object as a dryrun.
    """

    config = rh_config.rns_client.load_config(name, load_from=load_from)
    # type = config.pop('resource_type', 'send')
    config['name'] = name or config.get('rns_address', None) or config.get('name')

    # TODO handle case where package has been loaded from rns and we don't need to sync default package.
    # TODO [DG] Do we need to append './' if we detect a local function but the workdir
    # path is not given?

    config['reqs'] = reqs if reqs is not None else config.get('reqs', ['./'])

    processed_reqs = []
    for req in config['reqs']:
        if isinstance(req, str) and req[0] == '/' and \
                rh_config.rns_client.exists(req, load_from=load_from):
            # If req is an rns address
            req = rh_config.rns_client.load_config(req, load_from=load_from)
        processed_reqs.append(req)
    config['reqs'] = processed_reqs

    if fn:
        fn_pointers = Send.extract_fn_paths(raw_fn=fn, reqs=config['reqs'])
        if fn_pointers[1] == 'notebook':
            if serialize_notebook_fn:
                class FakeModule:
                    pass

                setattr(FakeModule, fn.__name__, fn)
                # TODO name this after the notebook, not the send
                serialization_dir = Path.cwd() / (f'{config["name"]}_fn' if config['name'] else 'send_fn')
                serialization_dir.mkdir(exist_ok=True, parents=True)
                pickled_package = Package(name=serialization_dir.stem,
                                          url=str(serialization_dir),
                                          install_method='unpickle')
                # TODO name this after the send
                pickled_package.put({'functions.pickle': pickle.dumps(FakeModule)})
                config['reqs'].append(pickled_package)
                fn_pointers = (fn_pointers[0], pickled_package.name, fn_pointers[2])
            else:
                # TODO put this in the current folder instead?
                module_path = Path.cwd() / (f'{config["name"]}_fn.py' if config['name'] else 'send_fn.py')
                logging.info(f'Writing out send function to {str(module_path)} as '
                             f'functions serialized in notebooks are brittle. Please make'
                             f'sure the function does not rely on any local variables, '
                             f'including imports (which should be moved inside the function body).')
                if not config['name']:
                    logging.warning('You should name Sends that are created in notebooks to avoid naming collisions '
                                    'between the modules that are created to hold their functions '
                                    '(i.e. "send_fn.py" errors.')
                source = inspect.getsource(fn).strip()
                with module_path.open('w') as f:
                    f.write(source)
                fn_pointers = (fn_pointers[0], module_path.stem, fn_pointers[2])
                # from importlib.util import spec_from_file_location, module_from_spec
                # spec = spec_from_file_location(config['name'], str(module_path))
                # module = module_from_spec(spec)
                # spec.loader.exec_module(module)
                # new_fn = getattr(module, fn_pointers[2])
                # fn_pointers = Send.extract_fn_paths(raw_fn=new_fn, reqs=config['reqs'])
        config['fn_pointers'] = fn_pointers

    config['hardware'] = hardware or config.get('hardware') or Send.DEFAULT_HARDWARE
    if isinstance(config['hardware'], str):
        hw_dict = rh_config.rns_client.load_config(config['hardware'], load_from=load_from)
        if not hw_dict:
            raise RuntimeError(f'Hardware {config["hardware"]} not found locally or in RNS.')
        config['hardware'] = hw_dict

    config['image'] = image or config.get('image')

    config['access_level'] = config.get('access_level', Send.DEFAULT_ACCESS)
    config['save_to'] = save_to

    new_send = Send.from_config(config, dryrun=dryrun)

    if load_secrets and not dryrun:
        new_send.send_secrets()

    if new_send.name:
        new_send.save()

    return new_send
