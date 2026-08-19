"""
Descarga GDAS/FNL 0.25 historico via NCAR GDEX (sucesor de RDA, dataset
d083003). Reemplaza al gdas_lib.py viejo, que usaba el login/API de
rda.ucar.edu -- ese endpoint ya no existe (404, migro a gdex.ucar.edu).

Auth: token de usuario (no usuario/contraseña), se consigue logueado en
https://gdex.ucar.edu/accounts/profile/ y se guarda en
resources/passwords.cfg, seccion [GDEX], campo token.
"""
import os
import json
import time
import tarfile
import logging
import datetime
import urllib.request
from configparser import ConfigParser
from get_weather import GDAS_FOLDER

BASE_URL = 'https://gdex.ucar.edu/api/'
DSID = 'd083003'
CONTROL_TEMPLATE = 'resources/ds083.3_control_file.template'
LOG_FILENAME = 'logs/get_weather.log'
os.makedirs(os.path.dirname(LOG_FILENAME), exist_ok=True)
logging.basicConfig(format='%(levelname)s: %(asctime)s %(message)s', filename=LOG_FILENAME, level=logging.DEBUG)


def get_token():
    c = ConfigParser()
    c.read('resources/passwords.cfg')
    return c.get('GDEX', 'token')


def _get(path, token):
    req = urllib.request.Request(f'{BASE_URL}{path}?token={token}')
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _post(path, body, token):
    req = urllib.request.Request(
        f'{BASE_URL}{path}?token={token}',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-type': 'application/json'},
        method='POST',
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def build_control(start_date, end_date):
    template = open(CONTROL_TEMPLATE).read().format(
        start_date=start_date.strftime('%Y%m%d0000'),
        end_date=end_date.strftime('%Y%m%d0000'),
    )
    control = {}
    for line in template.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, value = line.split('=', 1)
        control[key] = value
    control['dataset'] = DSID
    return control


def submit(start_date, end_date):
    """Envia el pedido de subset. Devuelve el request_index (string)."""
    token = get_token()
    control = build_control(start_date, end_date)
    result = _post('submit/', control, token)
    if result.get('error_messages'):
        raise GDASError(f"Error al enviar el pedido: {result['error_messages']}")
    request_id = result['data']['request_id']
    logging.info('Pedido GDEX enviado: %s' % request_id)
    return request_id


def get_status(request_id):
    token = get_token()
    result = _get(f'status/{request_id}/', token)
    return result['data']['status']


def waitFor(request_id, max_wait_minutes=180, poll_seconds=60):
    """Espera (polling) a que el pedido termine de procesarse."""
    waited = 0
    while waited < max_wait_minutes * 60:
        status = get_status(request_id)
        if status not in ('Queued for Processing', 'Processing'):
            return status
        logging.info('Esperando pedido %s (status=%s)' % (request_id, status))
        time.sleep(poll_seconds)
        waited += poll_seconds
    raise GDASError(f'Timeout esperando el pedido {request_id} despues de {max_wait_minutes} min')


def get_filelist(request_id):
    token = get_token()
    result = _get(f'get_req_files/{request_id}/', token)
    return result['data']['web_files']


def download(request_id, folder=GDAS_FOLDER):
    """Descarga y desempaqueta los .tar del pedido en `folder`. Idempotente:
    si un archivo grib2 ya existe, no vuelve a bajar/desempaquetar su .tar."""
    files = get_filelist(request_id)
    already_have = set(os.listdir(folder)) if os.path.isdir(folder) else set()
    pending = [f for f in files if f['wfile'] not in already_have]
    tar_urls = sorted(set(f['web_path'] for f in pending))

    os.makedirs(folder, exist_ok=True)
    tmp_dir = folder + '/.tmp_tar/'
    os.makedirs(tmp_dir, exist_ok=True)

    for i, tar_url in enumerate(tar_urls, 1):
        tar_name = tar_url.rsplit('/', 1)[-1]
        tar_path = tmp_dir + tar_name
        print(f'[{i}/{len(tar_urls)}] Descargando {tar_name}')
        urllib.request.urlretrieve(tar_url, tar_path)
        with tarfile.open(tar_path) as t:
            t.extractall(folder)
        os.remove(tar_path)
        logging.info('Descargado y desempaquetado: %s' % tar_name)

    if os.path.isdir(tmp_dir) and not os.listdir(tmp_dir):
        os.rmdir(tmp_dir)
    print(f'Listo: {len(files)} archivos grib2 en {folder} ({len(tar_urls)} tars nuevos)')


def purge(request_id):
    token = get_token()
    req = urllib.request.Request(f'{BASE_URL}purge/{request_id}/?token={token}', method='DELETE')
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def downloadData(start_date, end_date, folder=GDAS_FOLDER):
    request_id = submit(start_date, end_date)
    waitFor(request_id)
    download(request_id, folder)
    return request_id


class GDASError(Exception):
    pass


if __name__ == '__main__':
    import sys
    FORMAT = '%Y-%m-%d'
    if len(sys.argv) > 2:
        start_date, end_date = (
            datetime.datetime.strptime(sys.argv[1], FORMAT).date(),
            datetime.datetime.strptime(sys.argv[2], FORMAT).date(),
        )
    else:
        start_date, end_date = datetime.date.today() - datetime.timedelta(days=1), datetime.date.today()

    print(f'Pedido GDAS/FNL (GDEX) {start_date} -> {end_date}')
    downloadData(start_date, end_date)
