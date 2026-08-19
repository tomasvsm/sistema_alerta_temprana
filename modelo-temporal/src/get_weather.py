import os
import re
import sys
import time
import urllib
import logging
import datetime
import pygrib
import numpy as np
from os import path
import netCDF4 as nc
import http.cookiejar
from configparser import ConfigParser
import multiprocessing as mp
from tqdm import tqdm

DATA_FOLDER='data/public/'
IMERG_FOLDER=DATA_FOLDER+'/imerg/'
#IMERG_FOLDER='/media/data/msgro/dengue/data/public/imerg/'
GDAS_FOLDER=DATA_FOLDER+'/gdas/'
#GDAS_FOLDER='/media/msgro/b007d8ea-4b16-46f5-9697-66f6dea8591e/msgro/dengue/data/public/gdas/new/'
FORECAST_FOLDER=DATA_FOLDER+'/forecast/'
FORECAST_PGB_FOLDER=FORECAST_FOLDER+'/pgb/'
FORECAST_FLX_FOLDER=FORECAST_FOLDER+'/flx/'
FORECAST_RANGE=5 #60#in days
HISTORY_FOLDER=DATA_FOLDER+'/.history/'
SLEEP=1
LOG_FILENAME='logs/get_weather.log'

logging.basicConfig(format='%(levelname)s: %(asctime)s %(message)s',filename=LOG_FILENAME,level=logging.DEBUG)

## UTILS ##
def daterange(start_date, end_date):
    for n in range(int ((end_date - start_date).days)):
        yield start_date + datetime.timedelta(n)

def getLocations():
    config_parser = ConfigParser()
    config_parser.read('resources/get_weather.cfg')
    #config_parser.read('resources/coordinates_smn.cfg')
    return config_parser.sections()

def getStartEndDates(filename):
    dates=[line.split(',')[0] for line in open(filename,'r').readlines()]
    dates=[date for date in dates if date]
    return datetime.datetime.strptime(dates[1], '%Y-%m-%d').date(),datetime.datetime.strptime(dates[-1], '%Y-%m-%d').date()

def check_file_exists(folder,filename):
    if(path.isfile(folder+filename)):
        print(f"{filename} already exists")
        return True
    return False

def download_file(url, folder, filename):
    try:
        request = urllib.request.Request(url)
        response = urllib.request.urlopen(request)
        if not response.headers['Content-Length']:
            print(f"Content-Length doesn't exists for {filename}")
            return False
        total_size = int(response.headers['Content-Length'])
        downloaded_size = 0
        block_size = 8192
        with open(os.path.join(folder,filename), 'wb') as file:
            with tqdm(total=total_size, unit='B', unit_scale=True, desc=f'Downloading {filename}') as progress_bar:
                while True:
                    buffer = response.read(block_size)
                    if not buffer:
                        break
                    downloaded_size += len(buffer)
                    file.write(buffer)
                    progress_bar.update(len(buffer))
        print("Download complete!")
        return True
    except Exception as e:
        print(f"Error downloading {filename} from {url}")
        print(e)
        return False
## END UTILS ##

## IMERG ##
def getIMERGVersion(a_date):
    if(a_date < datetime.date(2019,5,1)):
        return '07'
    elif(a_date < datetime.date(2024,6,1)):
        return '07'
    else:
        return '07'

# GES DISC va incrementando la letra de version menor (B, C, ...) del run
# "Late" sin re-procesar fechas viejas, asi que no hay forma de saber de
# antemano cual le corresponde a una fecha dada: se prueban en orden hasta
# que una exista (confirmado 2026-08-19: enero/2025 = V07B, junio/2026 = V07C).
#
# OJO A FUTURO: GES DISC tiene un alert activo (19/05/2026) de transicion de
# IMERG a V08. A la fecha de este comentario "GPM_3IMERGDL.08" (Late, diario,
# la coleccion que usamos) todavia NO existe -- pero si en algun momento deja
# de encontrar archivos con estos sufijos, revisar si ya migro. Ahi habria que
# cambiar tambien el major version en getIMERGVersion(), no solo la letra.
IMERG_MINOR_VERSIONS = ['C', 'B']

#TODO: take into account the utc time. ?

def getFilenameForIMERG(a_date, minor_version):
    version = getIMERGVersion(a_date) + minor_version
    year = a_date.year
    month = a_date.month
    day = a_date.day
    return f'3B-DAY-L.MS.MRG.3IMERG.{year}{month:02d}{day:02d}-S000000-E235959.V{version}.nc4'

def findLocalIMERGFilename(folder, a_date):
    """Nombre del archivo ya descargado para esta fecha, probando las versiones conocidas. None si no hay ninguno."""
    for mv in IMERG_MINOR_VERSIONS:
        filename = getFilenameForIMERG(a_date, mv)
        if path.isfile(folder + filename):
            return filename
    return None

#https://wiki.earthdata.nasa.gov/display/EL/How+To+Access+Data+With+Python
def downloadDataFromIMERG(start_date,end_date,folder):
    config_parser = ConfigParser()
    config_parser.read('resources/passwords.cfg')
    passman = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    passman.add_password(None, 'https://urs.earthdata.nasa.gov',config_parser.get('IMERG','username'), config_parser.get('IMERG','password'))
    opener = urllib.request.build_opener(urllib.request.HTTPBasicAuthHandler(passman),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    urllib.request.install_opener(opener)
    for a_date in daterange(start_date,end_date):
        if findLocalIMERGFilename(folder, a_date):
            print(f"{getFilenameForIMERG(a_date, IMERG_MINOR_VERSIONS[0])} (u otra version) already exists")
            continue

        year = a_date.year
        month = a_date.month
        version = getIMERGVersion(a_date)

        downloaded = False
        for mv in IMERG_MINOR_VERSIONS:
            filename = getFilenameForIMERG(a_date, mv)
            url = f'https://gpm1.gesdisc.eosdis.nasa.gov/data/GPM_L3/GPM_3IMERGDL.{version}/{year}/{month:02d}/{filename}'

            #version 5 is different
            if(version == '05'):
                url = url.replace('data','opendap')+'.nc4?precipitationCal[1040:1280][339:709],precipitationCal_cnt[1040:1280][339:709],lon[1040:1280],lat[339:709]'

            if download_file(url, folder, filename):
                downloaded = True
                break
            time.sleep(SLEEP)

        if not downloaded:
            print(f"No se pudo descargar IMERG para {a_date} con ninguna version conocida {IMERG_MINOR_VERSIONS}")

def extractDailyDataFromIMERG(lat,lon,a_date):
    filename = findLocalIMERGFilename(IMERG_FOLDER, a_date)
    if filename is None:
        raise FileNotFoundError(f"No hay archivo IMERG local para {a_date} (versiones probadas: {IMERG_MINOR_VERSIONS})")
    nc_filename=IMERG_FOLDER+filename
    grp = nc.Dataset(nc_filename)
    lats = grp.variables['lat'][:]
    lons = grp.variables['lon'][:]
    if getIMERGVersion(a_date) == "07":
        precipitation_variable = 'precipitation'
    else:
        precipitation_variable = 'precipitationCal'
    precipitations=grp.variables[precipitation_variable]

    if(getIMERGVersion(a_date)=='05'):
        p=precipitations[(abs(lons-lon)).argmin(),(abs(lats-lat)).argmin()]
    else:#version 6 and 7
        p=precipitations[0,(abs(lons-lon)).argmin(),(abs(lats-lat)).argmin()]#TODO:search documentation on the first index(time)
    grp.close()
    return p
## END IMERG ##

## GDAS ##
#TODO: take into account the utc time.
def getFilenameForGDAS(a_date,a_time,forecast):
    year = a_date.year
    month = a_date.month
    day = a_date.day
    return f'gdas1.fnl0p25.{year}{month:02d}{day:02d}{a_time}.f{forecast}.grib2'

def downloadDataFromGDAS(start_date,end_date,folder):
    for a_date in daterange(start_date,end_date):
        for a_time in ['00','06','12','18']:
            for a_forecast in ['00','03','06','09']:#a forcast time
                filename=getFilenameForGDAS(a_date,a_time,a_forecast)
                
                if check_file_exists(folder,filename):
                    continue

                base_url = 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gdas_0p25.pl'
                opts = {
                    'file':f'gdas.t{a_time}z.pgrb2.0p25.f0{a_forecast}',
                    'lev_2_m_above_ground':'on',
                    'var_GUST':'on',
                    'var_RH':'on',
                    'var_TCDC':'on',
                    'var_TMAX':'on',
                    'var_TMIN':'on',
                    'var_TMP':'on',
                    'subregion':'on',
                    'leftlon':'-76',
                    'rightlon':'-52',
                    'toplat':'-19',
                    'bottomlat':'-56',
                    'dir':f'%2Fgdas.{a_date.year}{a_date.month:02d}{a_date.day:02d}%2F{a_time}%2Fatmos' # type: ignore
                }

                url = f'{base_url}?{"&".join([f"{k}={v}" for k,v in opts.items()])}'

                download_file(url, folder, filename)
                time.sleep(1)


def extractDailyDataFromGDAS(lat,lon,a_date,folder,FIELDS,typeOfLevel,f):
    TIMES=['00','06','12','18']
    epsilon=0.5#TODO:avoid this
    fields_values= dict( (field,[]) for field in FIELDS)
    for a_time in TIMES:
        grib_filename=folder+getFilenameForGDAS(a_date,a_time,f)
        if(a_time=='00'): grib_filename=folder+getFilenameForGDAS(a_date + datetime.timedelta(days=1),a_time,f)#utc hack
        if(not os.path.isfile(grib_filename)):
            logging.warning('%s not found, but keep going anyways'%grib_filename)
            continue
        grbs=pygrib.open(grib_filename)
        for field in FIELDS:
            grb = grbs.select(parameterName=field,typeOfLevel=typeOfLevel)[0]
            assert (grb.validDate - datetime.timedelta(hours=3,seconds=1)).date() == a_date, '%s vs %s for %s'%( (grb.validDate - datetime.timedelta(hours=3,seconds=1)).date(),a_date,grib_filename) #the second is because I want to take into account the 00 of the next day
            #validate lat,lon
            lats, lons = grb.latlons()
            assert lats.min()<=lat<=lats.max() and lons.min()<=lon<=lons.max(), '%s<%s<%s  %s<%s<%s for %s'%(lats.min(),lat,lats.max(),lons.min(),lon,lons.max(), grib_filename)
            #extract the data
            data, lats, lons = grb.data(lat1=lat-epsilon,lat2=lat+epsilon,lon1=lon-epsilon,lon2=lon+epsilon)#TODO:use lat,lon to fabricate lat1,lat2,lon1,lon2
            value=data[0,0]
            if(grb['units']=='K'): value-=273.15 #Kelvin->C
            fields_values[field]+=[ value ]#check this!
        grbs.close()
    #day ended
    return fields_values
## END GDAS ##

## FORECAST ##
def downloadForecast():
    #download
    start_date=datetime.date.today()
    end_date=start_date+datetime.timedelta(days=FORECAST_RANGE)
    f='00'
    forecast_types={
    FORECAST_FLX_FOLDER:'http://nomads.ncep.noaa.gov/cgi-bin/filter_cfs_flx.pl?file=flxf{f_year}{f_month:02}{f_day:02}{time}.01.{year}{month:02}{day:02}{f}.grb2&lev_2_m_above_ground=on&lev_surface=on&var_PRATE=on&var_TMAX=on&var_TMIN=on&var_TMP=on&subregion=&leftlon=-76&rightlon=-52&toplat=-19&bottomlat=-56&dir=%%2Fcfs.{year}{month:02}{day:02}%%2F{f}%%2F6hrly_grib_01',
    FORECAST_PGB_FOLDER:'http://nomads.ncep.noaa.gov/cgi-bin/filter_cfs_pgb.pl?file=pgbf{f_year}{f_month:02}{f_day:02}{time}.01.{year}{month:02}{day:02}{f}.grb2&lev_2_m_above_ground=on&lev_surface=on&var_APCP=on&var_RH=on&subregion=&leftlon=-76&rightlon=-52&toplat=-19&bottomlat=-56&dir=%%2Fcfs.{year}{month:02}{day:02}%%2F{f}%%2F6hrly_grib_01'
    }
    for key in forecast_types.keys():
        for a_date in daterange(start_date,end_date):
            for a_time in ['00','06','12','18']:
                url=forecast_types[key].format(year=start_date.year,month=start_date.month,day=start_date.day, f_year=a_date.year,f_month=a_date.month,f_day=a_date.day,time=a_time,f=f)
                folder = key
                filename = getFilenameForGDAS(a_date,a_time,f)
                download_file(url, folder, filename)
                time.sleep(SLEEP)
## END FORECAST ##

def downloadData(start_date,end_date):
    logging.info('Downloading GDAS(fnl)')
    downloadDataFromGDAS(start_date,end_date,GDAS_FOLDER)
    logging.info('Downloading IMERG')
    downloadDataFromIMERG(start_date,end_date,IMERG_FOLDER)
    logging.info('Downloading forecast')
    downloadForecast()

def extractHistoricData(lat,lon,start_date,end_date,out_filename):
    output=''
    if(not os.path.isfile(out_filename)):
        output='Date,Minimum Temp (C),Mean Temperature (C),Maximum Temp (C),Rain (mm),Relative Humidity %,CloudCover,Mean Wind SpeedKm/h' + '\n'
        if os.path.isfile(DATA_FOLDER+'cordoba.csv'):
            first_date,last_date=getStartEndDates(DATA_FOLDER+'cordoba.csv')
        else:
            first_date,last_date=start_date,end_date
        start_date=min(start_date,first_date)#in case this is a new city, we start from the very beginning
    for a_date in daterange(start_date,end_date):
        FIELDS=['Minimum temperature','Maximum temperature','Relative humidity']
        #to validate that the +360 was ok: 1) gdal_translate a grib to a tif and open qgis with google map as background. 2) use https://www.latlong.net/Show-Latitude-Longitude.html 3)explore.py
        fields_values=extractDailyDataFromGDAS(lat,lon+360.,a_date,GDAS_FOLDER,FIELDS,typeOfLevel='heightAboveGround',f='03')
        try:
            min_T,max_T=np.min(fields_values[FIELDS[0]]),np.max(fields_values[FIELDS[1]])
            mean_T=(min_T+max_T)/2.
            mean_rh=(np.min(fields_values[FIELDS[2]])+np.max(fields_values[FIELDS[2]]))/2.
        except:
            print('Error in %s'%a_date)
            min_T = -99.99; max_T = -99.99; mean_T = -99.99; mean_rh = -99.99
            continue

        precipitation=extractDailyDataFromIMERG(lat,lon,a_date)
        output+=a_date.strftime('%Y-%m-%d')+', '+', '.join([str(min_T),str(mean_T),str(max_T),str(precipitation),str(mean_rh) ]) + ',,'+'\n'
    open(out_filename,'a').write(output)

def extractForecastData(lat,lon,out_filename):
    output='Date,Minimum Temp (C),Mean Temperature (C),Maximum Temp (C),Rain (mm),Relative Humidity %,CloudCover,Mean Wind SpeedKm/h' + '\n'
    today=datetime.date.today()
    for a_date in daterange(today,today+datetime.timedelta(hours=FORECAST_RANGE*24)):
        FIELDS=['Relative humidity']
        fields_values=extractDailyDataFromGDAS(lat,lon+360.,a_date,FORECAST_PGB_FOLDER,FIELDS,typeOfLevel='heightAboveGround',f='00')
        mean_rh=(np.min(fields_values[FIELDS[0]])+np.max(fields_values[FIELDS[0]]))/2.

        FIELDS=['Minimum temperature','Maximum temperature','Precipitation rate']
        fields_values=extractDailyDataFromGDAS(lat,lon+360.,a_date,FORECAST_FLX_FOLDER,FIELDS,typeOfLevel=['heightAboveGround','surface'],f='00')
        min_T,max_T=np.min(fields_values[FIELDS[0]]),np.max(fields_values[FIELDS[1]])
        mean_T=(min_T+max_T)/2.
        precipitation=np.sum(np.array(fields_values[FIELDS[2]])*60*60*6)
        output+=a_date.strftime('%Y-%m-%d')+', '+', '.join([str(min_T),str(mean_T),str(max_T),str(precipitation),str(mean_rh) ]) + ',,'+'\n'
    open(out_filename.replace('.csv','.forecast.csv'),'w').write(output)

def extractData(params):
    lat,lon,start_date,end_date,out_filename=params
    logging.info('Extracting data to %s'%out_filename)
    extractHistoricData(lat,lon,start_date,end_date,out_filename)
    extractForecastData(lat,lon,out_filename)

def joinFullWeather():
    for location in getLocations():
        historic_data=open(DATA_FOLDER+location+'.csv','r').read()
        forecast_data=open(DATA_FOLDER+location+'.forecast.csv','r').read()
        filename=location+'.full.csv'
        if(os.path.isfile(DATA_FOLDER+filename)): os.rename(DATA_FOLDER+filename,HISTORY_FOLDER+filename.replace('.csv','.weather-'+datetime.datetime.now().strftime('%Y-%m-%d')+'.csv'))#backup old results
        open(DATA_FOLDER+filename,'w').write(historic_data+ '\n'.join(forecast_data.split('\n')[1:]))#remove the header of forecast data

if(__name__ == '__main__'):

    print(f"Begin weather update: {datetime.datetime.now()}")

    FORMAT='%Y-%m-%d'
    #start_date,end_date=None,None
    start_date = datetime.date(2015, 7, 15)
    end_date = datetime.date(2024, 12, 31)
    if(len(sys.argv)>2):
        start_date,end_date= datetime.datetime.strptime(sys.argv[1],FORMAT).date(),datetime.datetime.strptime(sys.argv[2],FORMAT).date()
    elif(len(sys.argv)==1):
        today=datetime.date.today()
        print(f"Today's date: {today}")

        #first_date,last_date=getStartEndDates(DATA_FOLDER+'cordoba.csv')#in case the script failed, we start from the last good run.
        first_date,last_date=getStartEndDates(DATA_FOLDER+'/todos/fechas.csv')
        print(f"First date from file: {first_date}, Last date from file: {last_date}")
        #start_date,end_date= last_date+datetime.timedelta(1),today
        start_date, end_date = datetime.date(2015, 7, 15), datetime.date(2024, 12, 31)

    
    print(f"Range date: {start_date} - {end_date}")
    downloadData(start_date,end_date)
    config_parser = ConfigParser()
    config_parser.read('resources/get_weather.cfg')
    #config_parser.read('resources/coordinates_smn.cfg')
    params=[]
    for location in config_parser.sections():
        lat=float(config_parser.get(location,'lat'))
        lon=float(config_parser.get(location,'lon'))
        params=params+[[lat,lon,start_date,end_date,DATA_FOLDER+location+'.csv']]

    #for param in params:
    #    extractData(param)

    p = mp.Pool(mp.cpu_count() -1)
    vOpt=p.map(extractData, params)

    joinFullWeather()

    print(f"End weather update: {datetime.datetime.now()}")
