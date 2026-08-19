#coding: utf-8
import os
import sys
import utils
import datetime
import numpy as np
import scipy.stats as stats
import multiprocessing as mp
import similaritymeasures as sm
from config import Configuration
from collections import OrderedDict
from otero_precipitation import Model
from scipy.optimize import minimize,differential_evolution

MINIMIZE_METHOD='differential_evolution'
OVITRAP_FILENAME='data/private/ovitrampas_2017-2019.mean.csv'

RMSE,FRECHET,DTW,D='rmse','frechet','dtw','D'
OVI_FIT='out/equation_fitter/%s/_ovi%s.txt'

def distance(ovitrap_eggs_i,lwO):
    if(sys.argv[2]==RMSE):
        return utils.rmse(ovitrap_eggs_i[ovitrap_eggs_i!=[None]], lwO[ovitrap_eggs_i!=[None]])
    elif(sys.argv[2]==D):
        return utils.D(ovitrap_eggs_i[ovitrap_eggs_i!=[None]], lwO[ovitrap_eggs_i!=[None]])
    elif(sys.argv[2] in [FRECHET,DTW]):
        ovitrap_eggs_i=np.array(ovitrap_eggs_i,dtype=np.float)#this change None for np.nan
        valid_ovi_idx=~np.isnan(ovitrap_eggs_i)
        reversed_valid_ovi_idx=valid_ovi_idx[::-1]
        first,last=np.argmax(valid_ovi_idx), len(reversed_valid_ovi_idx)-np.argmax(reversed_valid_ovi_idx)-1
        x=np.array([[time_range[idx],lwO[idx]] for idx in range(first,last)])
        y=np.array([ [time_range[idx],ovitrap_eggs_i[idx] ] for idx,isValid in enumerate(valid_ovi_idx) if isValid])
        if(sys.argv[2]==FRECHET): return sm.frechet_dist(x,y)
        if(sys.argv[2]==DTW): return sm.dtw(x,y)[0]
    else:
        print('Metric %s not found'%sys.argv[2])
        quit()


def getConfiguration(x=None,domain=None):
    CONFIGURATION_FILENAME='resources/1c.cfg'
    if(x is None and domain is None): return Configuration(CONFIGURATION_FILENAME)#no arguments passed, return default configuration

    start_date,end_date=utils.getStartEndDates(OVITRAP_FILENAME)
    n=int(len(x)/len(domain))#not agnostic we assume all parameters have the same length
    configuration=Configuration(CONFIGURATION_FILENAME)
    for i,key in enumerate(domain):
        configuration.config_parser.set('breeding_site',key, ', '.join(map(str, x[i*n:(i+1)*n] )) )
    configuration.config_parser.set('simulation','end_date',str(end_date))

    return configuration

def calculateMetrics(time_range,model,ovitrap_eggs_i):
    BS_a,vBS_d,m,n,OVIPOSITION=model.parameters.BS_a,model.parameters.vBS_d,model.parameters.m,model.parameters.n,model.parameters.OVIPOSITION
    Y=model.Y
    indexOf=lambda t: (np.abs(time_range-t)).argmin()
    lwO=np.array([ (Y[indexOf(t),OVIPOSITION]-Y[indexOf(t-7),OVIPOSITION]).reshape(m,n).sum(axis=0) for t in time_range])/(BS_a*vBS_d)
    lwO=lwO[:,0]#if multiple container w assume the first one is ovitrap and the rest are wild containers
    d=utils.rmse(ovitrap_eggs_i[ovitrap_eggs_i!=[None]], lwO[ovitrap_eggs_i!=[None]])
    return d

def populate(time_range,start_date,ovitrap_eggs):
    result=np.array([None]*len(time_range))
    for date in ovitrap_eggs:
        d=(date-start_date).days
        eggs=utils.noneMean(ovitrap_eggs[date])
        result[(np.abs(time_range-d)).argmin()]=eggs
    return result

def error(x,ovitrap_eggs_i_with_id):
    #return np.dot(x,x)
    ovitrap_id,ovitrap_eggs_i,domain=ovitrap_eggs_i_with_id
    model=Model(getConfiguration(x,domain))#TODO:not agnostic
    ovitrap_eggs_i=populate(model.time_range,model.start_date,ovitrap_eggs_i)
    time_range,Y=model.solveEquations()
    error=calculateMetrics(time_range,model,ovitrap_eggs_i)
    #title='ovitrap:%s\nmf:%s\n' r'$\alpha_0$:%s' '\n' r'Error:%s $\rho$:%s p value:%s'%(ovitrap_id,model.parameters.vBS_mf.tolist(),model.parameters.vAlpha0.tolist(),error,rho,p_value)
    #print(title)
    return error

def getOptimalParameters(ovitrap_eggs_i_with_id):
    domain=ovitrap_eggs_i_with_id[2]
    configuration=getConfiguration()
    #initial value
    x0=np.concatenate( [configuration.getArray('breeding_site',key) for key in domain])
    #Σ vBS_d[i] = 1
    constraints = ()#({'type': 'eq', 'fun': lambda x:  1 - sum(x[0:-1])})#TODO:not agnostic
    #0<=x<=1,0<=ws_s<=1.
    bounds=tuple()
    for key in domain: bounds+= tuple( [domain[key]]* len(configuration.getArray('breeding_site',key)) )

    if(MINIMIZE_METHOD=='SLSQP'):
        opt=minimize(error,x0,ovitrap_eggs_i_with_id,method='SLSQP',bounds=bounds,constraints=constraints,options={'eps': 1e-02, 'ftol': 1e-01})
    else:
        opt=differential_evolution(error,bounds,args=(ovitrap_eggs_i_with_id,))#, workers=mp.cpu_count()-2 for scipy>1.3

    open(OVI_FIT%(sys.argv[2],ovitrap_eggs_i_with_id[0]),'w').write(str(opt)+'\n'+str(domain)+'\n\n')#ovitrap_eggs_i_with_id[0] is the ovitrap_id

    return opt

if(__name__ == '__main__'):
    domain=OrderedDict({'height':(2,20),'manually_filled':(0,2),'bare':(0,1),'evaporation_factor':(0,2)})
    if(len(sys.argv)>1):
        ovitrap_id=int(sys.argv[1])
        if(os.path.isfile(OVI_FIT%(sys.argv[2],ovitrap_id))): quit() # if the ovitrap has been already fitted, skip
        if(not os.path.exists( (OVI_FIT%(sys.argv[2],'')).replace('/_ovi.txt','') ) ): os.makedirs( (OVI_FIT%(sys.argv[2],'')).replace('/_ovi.txt','') )#if folder doesn't exists, create it

        ovitrap_eggs_i_with_id=[ovitrap_id,utils.getOvitrapEggsFromCsv(OVITRAP_FILENAME,ovitrap_id),domain]
        vOpt=getOptimalParameters(ovitrap_eggs_i_with_id)
    else:
        ovitrap_eggs=[[ovitrap_id,utils.getOvitrapEggsFromCsv(OVITRAP_FILENAME,ovitrap_id),domain] for ovitrap_id in range(1,152)]

        print('Starting...')
        vOpt=mp.Pool(mp.cpu_count()-2).map(getOptimalParameters, ovitrap_eggs)
        # vOpt=[]
        # for i,ovitrap_eggs_i_with_id in enumerate(ovitrap_eggs):
        #     vOpt+=getOptimalParameters(ovitrap_eggs_i_with_id)
        #     print('\r %s'%(round(float(i)/len(ovitrap_eggs)*100,2) ), end='')
        print(vOpt)
