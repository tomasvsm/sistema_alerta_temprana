import os
import sys

sys.path.append("src/")

import utils
import datetime
import numpy as np
from config import Configuration
from otero_precipitation import Model
from otero_precipitation_wrapper import Model as _Model
import time
import pandas as pd

DATA_PUBLIC = 'data/public'

def myConf():
    print('Creating a new configuration file')
    
    h = 10.
    location = 'cordoba'
    start_date = '2023-01-01'
    end_date = '2024-05-17'

    configuration = Configuration('resources/1c.cfg')
    configuration.config_parser.set('location','name',location)
    configuration.config_parser.set('simulation','start_date',start_date)
    configuration.config_parser.set('simulation','end_date',end_date)
    configuration.config_parser.set('breeding_site','height',str(h))
    configuration.config_parser.set('breeding_site','amount','1')

    configuration.save('myConf.cfg')
    return configuration

def _to_slice(seq_or_slice):
    """El motor C++ expone EGG/LARVAE/etc como un objeto seq(first,size) en vez
    de un slice de Python -- lo convertimos para poder indexar arrays igual
    que con el motor Python. Si ya es un slice (motor Python), lo deja igual."""
    if isinstance(seq_or_slice, slice):
        return seq_or_slice
    first, size = seq_or_slice.first(), seq_or_slice.size()
    return slice(first, first + size)

def main(confFile=None,outputFile=None,engine='cpp'):
    assert engine in ('cpp','python')

    if confFile is not None:
        try:
            configuration = Configuration(confFile)
        except FileNotFoundError:
            print('Configuration file not found')
            configuration = myConf()
            confFile = 'myConf.cfg'
    else:
        configuration = myConf()
        confFile = 'myConf.cfg'

    t1 = time.time()
    if engine == 'cpp':
        model = _Model(confFile)
        time_range, results = model.solveEquations()
        time_range = np.array(time_range)
        results = np.array(results)
    else:
        model = Model(configuration)
        time_range, results = model.solveEquations()
    t2 = time.time()
    print('Elapsed time: ', t2-t1)

    indexOf = lambda t: (np.abs(time_range-t)).argmin()

    start_datetime = datetime.datetime.strptime(configuration.getString('simulation','start_date'),'%Y-%m-%d')
    end_datetime = datetime.datetime.strptime(configuration.getString('simulation','end_date'),'%Y-%m-%d')
    dates = [(start_datetime + datetime.timedelta(days=t)) for t in time_range]

    EGG    = _to_slice(model.parameters.EGG)
    LARVAE = _to_slice(model.parameters.LARVAE)
    PUPAE  = _to_slice(model.parameters.PUPAE)
    ADULT1 = model.parameters.ADULT1
    ADULT2 = model.parameters.ADULT2
    WATER  = _to_slice(model.parameters.WATER)
    OVIPOSITION = _to_slice(model.parameters.OVIPOSITION)
    BS_a   = model.parameters.BS_a

    E = np.sum(results[:,EGG],axis=1)/BS_a
    L = np.sum(results[:,LARVAE],axis=1)/BS_a
    Pu = np.sum(results[:,PUPAE],axis=1)/BS_a
    A = (results[:,ADULT1]+results[:,ADULT2])/BS_a

    lwO = np.array([results[indexOf(t),OVIPOSITION] - results[indexOf(t-7),OVIPOSITION] for t in time_range])
    lwO_mean = np.array([lwO[indexOf(t-7):indexOf(t+7)].mean(axis=0) for t in time_range])
    O = np.sum(lwO_mean,axis=1)/BS_a

    if engine == 'cpp':
        # weather.T/RH del motor C++ no estan vectorizadas (toman un float, no un array)
        T = np.array([model.parameters.weather.T(t) for t in time_range]) - 273.15
        RH = np.array([model.parameters.weather.RH(t) for t in time_range])
        location = model.parameters.location  # string, no dict
    else:
        T = model.parameters.weather.T(time_range) - 273.15 # Convert to Celsius
        RH = model.parameters.weather.RH(time_range)
        location = model.parameters.location['name']

    # extract precipitation data from csv file
    location_filename = os.path.join(DATA_PUBLIC,f'{location}.csv')
    P = utils.getPrecipitationsFromCsv(location_filename,start_datetime.date(),end_datetime.date())
    
    # Save results to csv file -- nombres de columna consistentes con
    # validacion/output_modelo/*_modelo.csv (mismo formato, se agrega pupae)
    df = pd.DataFrame({
        'date': dates,
        'egg': E,
        'larvae': L,
        'pupae': Pu,
        'adult1+adult2': A,
        'oviposition': O,
        'precipitations': P,
        'temperature': T,
        'rh': RH,
    })
    df.set_index('date',inplace=True)
    if not outputFile:
        outputFile = 'results.csv'
    df.to_csv(outputFile,index=True)

if(__name__ == '__main__'):
    main()
