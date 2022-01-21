import yaml
import time
import datetime
import threading
import os
import re
import urllib
from base64 import b64encode
from datetime import timezone
from jinja2 import Environment, FileSystemLoader, select_autoescape
from kubernetes import client, config
from notebook.auth.security import passwd
from portal import logger
from portal import app

templates = Environment(loader=FileSystemLoader("portal/yaml"), autoescape=select_autoescape())
namespace = app.config.get("NAMESPACE")
gpu_available = app.config.get("GPU_AVAILABLE")

def load_kube_config():
    try:
        filename = app.config.get("KUBECONFIG")
        ingress_class = app.config.get("INGRESS_CLASS")
        domain_name = app.config.get("DOMAIN_NAME")
        if filename:
            config.load_kube_config(config_file = filename)
            logger.info("Loaded kubeconfig from file %s" %filename)
        else:
            config.load_kube_config()
            logger.info("Loaded default kubeconfig file")
        logger.info("Using namespace %s" %namespace)
        logger.info("Using domain name %s" %domain_name)
        logger.info("GPU is available as a resource" if gpu_available == 'Available' else "GPU is not available as a resource")
        logger.info("Using kubernetes.io/ingress.class %s" %ingress_class)
    except:
        logger.error("Error loading kubeconfig")
        config.load_kube_config()

def start_notebook_manager():
    t = threading.Thread(target=manage_notebooks)
    t.start()
    logger.info("Started k8s notebook manager")

def manage_notebooks():
    time.sleep(60)
    while True:
        pods = get_pods()
        count = 0
        for pod in pods:
            notebook_name = pod.metadata.name
            if has_notebook_expired(pod):
                logger.info("Notebook %s in namespace %s has expired" %(notebook_name, namespace))
                status = remove_notebook(notebook_name)
                if status:
                    count += 1
        logger.info("Removed %d notebooks during management cycle (1 cycle per hour)" %count)
        time.sleep(3600)

def supports_image(image):
    images = [
        'ivukotic/ml_platform:latest', 
        'ivukotic/ml_platform:conda', 
        'ivukotic/ml_platform_auto:latest', 
        'ivukotic/ml_platform_auto:conda', 
        'jupyter/minimal-notebook:latest'
    ]
    return image in images

def name_available(notebook_name):
    try: 
        core_v1_api = client.CoreV1Api()
        pods = core_v1_api.list_namespaced_pod(namespace, label_selector="instance=" + notebook_name)

        if not pods or len(pods.items) == 0:
            return True
    except:
        logger.error('Error checking whether notebook name %s is available' %notebook_name)
    return False

def generate_token():
    token_bytes = os.urandom(32)
    b64_encoded = b64encode(token_bytes).decode()
    return b64_encoded

def status_msg(status, message):
    return {'status': status, 'message': message}

def create_pod(notebook_name, username, password, cpu, memory, gpu, image, time_duration, password_hash=None, token=None):
    api = client.CoreV1Api()
    template = templates.get_template("pod.yaml")
    cpu_limit = cpu * 2
    memory_limit = memory * 2
    pod = yaml.safe_load(template.render(namespace=namespace, 
                                            notebook_name=notebook_name, 
                                            username=username, 
                                            password=password, 
                                            password_hash=password_hash, 
                                            token=token, 
                                            cpu_request=cpu, 
                                            memory_request=f"{memory}Gi", 
                                            gpu_request=gpu,
                                            cpu_limit=cpu_limit,
                                            memory_limit=f"{memory_limit}Gi",
                                            gpu_limit=gpu,
                                            gpu_available=gpu_available, 
                                            image=image, 
                                            days=time_duration))                           
    api.create_namespaced_pod(namespace=namespace, body=pod)
    # logger.info("Created pod %s" %notebook_name)

def create_service(notebook_name, image):
    api = client.CoreV1Api()
    template = templates.get_template("service.yaml")
    service = yaml.safe_load(template.render(namespace=namespace, notebook_name=notebook_name, image=image))
    api.create_namespaced_service(namespace=namespace, body=service)
    # logger.info("Created service %s" %notebook_name)

def create_ingress(notebook_name, username, image):
    api = client.NetworkingV1Api()
    domain_name = app.config['DOMAIN_NAME']
    # logger.info("Creating subdomain %s on domain %s" %(notebook_name, domain_name))
    ingress_class = app.config['INGRESS_CLASS']
    template = templates.get_template("ingress.yaml")
    ingress = yaml.safe_load(template.render(domain_name=domain_name, ingress_class=ingress_class, namespace=namespace, notebook_name=notebook_name, username=username, image=image))
    api.create_namespaced_ingress(namespace=namespace,body=ingress)
    # logger.info("Created ingress %s" %notebook_name)

def create_secret(notebook_name, username, token):
    api = client.CoreV1Api()
    template = templates.get_template("secret.yaml")
    sec = yaml.safe_load(template.render(namespace=namespace, notebook_name=notebook_name, username=username, token=token))
    api.create_namespaced_secret(namespace=namespace, body=sec)
    # logger.info("Created secret %s to store token %s" %(notebook_name, token))

def cpu_request_valid(cpu):
    if cpu >=1 and cpu <= 4:
        return True
    return False

def memory_request_valid(memory):
    if memory >=1 and memory <= 16:
        return True
    return False

def gpu_request_valid(gpu):
    if gpu >=0 and gpu <= 2:
        return True
    return False

def validate(notebook_name, username, password, cpu, memory, gpu, image, time_duration):
    if not supports_image(image):
        logger.warning('Docker image %s is not suppported' %image)
        return status_msg('warning', 'Docker image %s is not supported' %image)

    if not name_available(notebook_name):
        logger.warning('The name %s is already taken in namespace %s' %(notebook_name, namespace))
        return status_msg('warning', 'The name %s is already taken in namespace %s' %(notebook_name, namespace))

    if not cpu_request_valid(cpu):
        logger.warning('The CPU request %d is out of bounds' %cpu)
        return status_msg('warning', 'The CPU request %d is out of bounds' %cpu)

    if not memory_request_valid(memory):
        logger.warning('The memory request %d is out of bounds' %memory)
        return status_msg('warning', 'The memory request %d is out of bounds' %memory)

    if not gpu_request_valid(gpu):
        logger.warning('The GPU request %d is out of bounds' %gpu)
        return status_msg('warning', 'The GPU request %d is out of bounds' %gpu)

    return status_msg('success', 'Valid')

def create_notebook(notebook_name, username, password, cpu, memory, gpu, image, time_duration):
    status = validate(notebook_name, username, password, cpu, memory, gpu, image, time_duration)
    if status['status'] != 'success':
        return status

    password_hash = None
    token = None
    if password:
        password_hash = passwd(password)
        # logger.info("Using password based authentication for notebook %s" %notebook_name)
    else:
        token = generate_token()
        # logger.info("Using token based authentication for notebook %s" %notebook_name)
        logger.info("The token for %s is %s" %(notebook_name, token))

    try:           
        create_pod(notebook_name, username, password, cpu, memory, gpu, image, time_duration, password_hash, token)
        create_service(notebook_name, image)
        create_ingress(notebook_name, username, image)

        if token:
            create_secret(notebook_name, username, token)

        logger.info('Created notebook %s' %notebook_name)
        return status_msg('success', 'Created notebook %s' %notebook_name)
    except:
        logger.error('Error creating notebook %s' %notebook_name)
        return status_msg('warning', 'Error creating notebook %s' %notebook_name)

def get_creation_date(pod):
    try:
        return pod.metadata.creation_timestamp
    except:
        logger.error('Error getting creation date for notebook %s in namespace %s' %(pod.metadata.name, namespace))
        return None

def get_creation_timestamp(pod):
    creation_date = get_creation_date(pod)
    if creation_date:
        return creation_date.timestamp()
    return -1

def get_expiration_date(pod):
    try:
        if hasattr(pod.metadata, 'labels') and 'time2delete' in pod.metadata.labels:
            cr_ts = pod.metadata.creation_timestamp
            td_str = pod.metadata.labels['time2delete']
            pattern = re.compile(r"ttl-\d+")
            if pattern.match(td_str):
                td = int(td_str.split("-")[1])
                expiration_date = cr_ts + datetime.timedelta(td)
                return expiration_date
    except:
        logger.error('Error getting expiration date for notebook %s in namespace %s' %(pod.metadata.name, namespace))
    return None

def get_expiration_timestamp(pod):
    expiration_date = get_expiration_date(pod)
    if expiration_date:
        return expiration_date.timestamp()
    return -1

def has_notebook_expired(pod):
    exp_date = get_expiration_date(pod)
    if exp_date:
        return datetime.datetime.now(timezone.utc) > exp_date
    return False

def is_notebook_closing(pod):
    if pod.metadata.deletion_timestamp:
        return True
    return False

def get_notebook_status(pod):
    notebook_name = pod.metadata.name
    try: 
        if is_notebook_closing(pod):
            return 'Removing'
        if get_pod_status(pod) == 'Running':
            core_v1_api = client.CoreV1Api()
            log = core_v1_api.read_namespaced_pod_log(notebook_name, namespace=namespace)
            if re.search("Jupyter Notebook.*is running at.*", log) or re.search("Jupyter Server.*is running at.*", log):
                return 'Ready'
            else:
                return 'Loading'
    except:
        logger.error('Error getting status for notebook %s' %notebook_name)
    return 'Not ready'

def get_pod_status(pod):
    if is_notebook_closing(pod):
        return '--'
    return pod.status.phase

def get_certificate_status(pod):
    if is_notebook_closing(pod):
        return '--'
    try:
        net = client.NetworkingV1Api()
        notebook_name = pod.metadata.name
        ingress = net.read_namespaced_ingress(notebook_name, namespace)
        secretName = ingress.spec.tls[0].secret_name
        objs = client.CustomObjectsApi()
        cert = objs.get_namespaced_custom_object(
            group = "cert-manager.io",
            version = "v1",
            name = secretName,
            namespace = namespace,
            plural = "certificates"
        )   
        for condition in cert['status']['conditions']:
            if condition['type'] == 'Ready':    
                return 'Ready' if condition['status'] == 'True' else 'Not ready'
    except:
        logger.error("Error getting certificate status for notebook %s" %notebook_name)
    return 'Unknown'

def has_token(notebook_name):
    try:
        api = client.CoreV1Api()
        secs = api.list_namespaced_secret(namespace)
        for sec in secs.items:
            if sec.metadata.name == notebook_name and 'token' in sec.data:
                return True
        return False
    except:
        logger.error("Error getting secret for notebook %s" %notebook_name)
        return False

def get_token(notebook_name):
    try:
        api = client.CoreV1Api()
        sec = api.read_namespaced_secret(notebook_name, namespace)
        return sec.data['token']
    except:
        logger.error("Error getting secret for notebook %s" %notebook_name)
        return None

def get_url(pod):
    if is_notebook_closing(pod):
        return None
    try: 
        net = client.NetworkingV1Api()
        notebook_name = pod.metadata.name
        ingress = net.read_namespaced_ingress(notebook_name, namespace)
        url = 'https://' + ingress.spec.rules[0].host
        if has_token(notebook_name):
            token = get_token(notebook_name)
            url += "?"
            url += urllib.parse.urlencode({'token': token})
        # logger.info("URL for notebook %s is %s" %(notebook_name, url))
        return url
    except:
        logger.error('Error getting URL for pod %s' %notebook_name)
        return None

def get_pods():
    try:
        core_v1_api = client.CoreV1Api()
        pods = core_v1_api.list_namespaced_pod(namespace)
        # logger.info("Read %d pods from namespace %s" %(len(pods.items), namespace))
        if pods.items:
            return pods.items
        return []
    except:
        return None

def get_user_pods(username):
    user_pods = []
    try:
        core_v1_api = client.CoreV1Api()
        pods = core_v1_api.list_namespaced_pod(namespace)
        for pod in pods.items:
            try: 
                if pod.metadata.labels['owner'] == username:   
                    user_pods.append(pod)
            except:
                logger.info('Error processing pod %s' %pod.metadata.name)
    except:
        logger.error('Error getting pods')
    # logger.info("Read %d pods from namespace %s for user %s" %(len(user_pods), namespace, username))
    return user_pods

def get_notebooks(username):
    user_pods = get_user_pods(username)
    notebooks = []
    for pod in user_pods:
        try: 
            # logger.info(pod)
            name = pod.metadata.name
            # logger.info("Read name for pod %s in namespace %s" %(name, namespace))
            url = get_url(pod)
            # logger.info("Read URL for notebook %s" %name)
            creation_date = get_creation_timestamp(pod)
            # logger.info("Creation timestamp for notebook %s: %d" %(name, creation_date))
            expiration_date = get_expiration_timestamp(pod)
            # logger.info("Expiration timestamp for notebook %s: %d" %(name, expiration_date))
            pod_status = get_pod_status(pod)
            # logger.info("Pod status for notebook %s: %s" %(name, pod_status))
            cert_status = get_certificate_status(pod)
            # logger.info("Certificate status for notebook %s: %s" %(name, cert_status))
            notebook_status = get_notebook_status(pod)
            # logger.info("Notebook status for notebook %s: %s" %(name, notebook_status))
            notebooks.append(
                {'name': name, 
                'namespace': namespace, 
                'username': username,
                'url': url,
                'pod_status': pod_status,
                'cert_status': cert_status,
                'notebook_status': notebook_status,
                'creation_date': creation_date,
                'expiration_date': expiration_date}
            )
            # logger.info("Retrieved notebook %s in namespace %s" %(name, namespace))
        except:
            logger.error('Error processing Jupyter notebook %s' %pod.metadata.name)
        
    return notebooks

def remove_notebook(notebook_name):
    try:
        core_v1_api = client.CoreV1Api()
        networking_v1_api = client.NetworkingV1Api()
        core_v1_api.delete_namespaced_pod(notebook_name, namespace)
        core_v1_api.delete_namespaced_service(notebook_name, namespace)
        networking_v1_api.delete_namespaced_ingress(notebook_name, namespace)
        if has_token(notebook_name):
            core_v1_api.delete_namespaced_secret(notebook_name, namespace)
        logger.info("Removing notebook %s in namespace %s" %(notebook_name, namespace))
        return True
    except:
        logger.error("Error removing notebook %s in namespace %s" %(notebook_name, namespace))
        return False

def remove_user_notebook(notebook_name, username):
    try:
        core_v1_api = client.CoreV1Api()
        networking_v1_api = client.NetworkingV1Api()
        pod = core_v1_api.read_namespaced_pod(notebook_name, namespace)
        if pod.metadata.labels['owner'] == username:
            core_v1_api.delete_namespaced_pod(notebook_name, namespace)
            core_v1_api.delete_namespaced_service(notebook_name, namespace)
            networking_v1_api.delete_namespaced_ingress(notebook_name, namespace)
            if has_token(notebook_name):
                core_v1_api.delete_namespaced_secret(notebook_name, namespace)
            logger.info('Removing notebook %s' %notebook_name)
            return status_msg('success', 'Removing notebook %s' %notebook_name)
        else:
            logger.warning('Notebook %s does not belong to user %s' %(notebook_name, username))
            return status_msg('warning', 'Notebook %s does not belong to user %s' %(notebook_name, username))
    except:
        logger.error(f"Error deleting pod {notebook_name} in namespace {namespace}")
        return status_msg('warning', 'Error removing notebook %s' %notebook_name)