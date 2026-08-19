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

def main(confFile=None,outputFile=None):

    if confFile is not None:
        try:
            configuration = Configuration(confFile)
        except FileNotFoundError:
            print('Configuration file not found')
            configuration = myConf()
    else:
        configuration = myConf()

    model = Model(configuration)

    t1 = time.time()
    time_range, results = model.solveEquations()
    t2 = time.time()
    print('Elapsed time: ', t2-t1)

    indexOf = lambda t: (np.abs(time_range-t)).argmin()

    start_datetime = datetime.datetime.strptime(configuration.getString('simulation','start_date'),'%Y-%m-%d')
    end_datetime = datetime.datetime.strptime(configuration.getString('simulation','end_date'),'%Y-%m-%d')
    dates = [(start_datetime + datetime.timedelta(days=t)) for t in time_range]

    EGG    = model.parameters.EGG
    LARVAE = model.parameters.LARVAE
    PUPAE  = model.parameters.PUPAE
    ADULT1 = model.parameters.ADULT1
    ADULT2 = model.parameters.ADULT2
    WATER  = model.parameters.WATER
    OVIPOSITION = model.parameters.OVIPOSITION
    BS_a   = model.parameters.BS_a

    E = np.sum(results[:,EGG],axis=1)/BS_a
    L = np.sum(results[:,LARVAE],axis=1)/BS_a
    A = (results[:,ADULT1]+results[:,ADULT2])/BS_a

    lwO = np.array([results[indexOf(t),OVIPOSITION] - results[indexOf(t-7),OVIPOSITION] for t in time_range])
    lwO_mean = np.array([lwO[indexOf(t-7):indexOf(t+7)].mean(axis=0) for t in time_range])
    O = np.sum(lwO_mean,axis=1)/BS_a

    T = model.parameters.weather.T(time_range) - 273.15 # Convert to Celsius
    RH = model.parameters.weather.RH(time_range)

    # extract precipitation data from csv file
    location = model.parameters.location['name']
    location_filename = os.path.join(DATA_PUBLIC,f'{location}.csv')
    P = utils.getPrecipitationsFromCsv(location_filename,start_datetime.date(),end_datetime.date())
    
    # Save results to csv file
    df = pd.DataFrame({'date':dates,'E':E,'L':L,'A':A,'O':O,'p':P,'T':T,'RH':RH})
    df.set_index('date',inplace=True)
    if not outputFile:
        outputFile = 'results.csv'
    df.to_csv(outputFile,index=True)

if(__name__ == '__main__'):
    main()
