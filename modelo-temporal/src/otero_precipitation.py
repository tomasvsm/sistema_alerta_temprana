#coding: utf-8
import scipy.integrate as spi
from scipy import interpolate
from config import Configuration
from weather import Weather
from bunch import Bunch
import numpy as np
import equations
import datetime
import utils
import rk

class Model:
    def __init__(self, configuration=Configuration('resources/otero_precipitation.cfg')):
        self.configuration=configuration

        self.parameters=Bunch()
        self.parameters.BS_a=configuration.getFloat('breeding_site','amount')
        self.parameters.BS_lh=configuration.getFloat('breeding_site','level_height')#in cm
        self.parameters.vBS_h=configuration.getArray('breeding_site','height')#in cm
        self.parameters.vBS_r=configuration.getArray('breeding_site','radius')#in cm
        self.parameters.vBS_s=configuration.getArray('breeding_site','surface')#in cm^2
        self.parameters.vBS_d=configuration.getArray('breeding_site','distribution')#distribution of BS. Sum must be equals to 1
        self.parameters.vBS_mf=configuration.getArray('breeding_site','manually_filled')#in cm
        self.parameters.vBS_b=configuration.getArray('breeding_site','bare')#in [0,1]
        self.parameters.vBS_ef=configuration.getArray('breeding_site','evaporation_factor')#in [0,2]
        self.parameters.n=len(self.parameters.vBS_d)
        self.parameters.m=int(np.max(np.ceil(self.parameters.vBS_h/self.parameters.BS_lh)))

        m,n=self.parameters.m,self.parameters.n
        self.parameters.EGG=slice(0,m*n)#in R^(mxn)
        self.parameters.LARVAE=slice(m*n,(1+m)*n)#in R^n
        self.parameters.PUPAE=slice((1+m)*n,(2+m)*n)#in R^n
        self.parameters.ADULT1=(2+m)*n#in R
        self.parameters.ADULT2=(2+m)*n+1#in R
        self.parameters.WATER=slice((2+m)*n+2,(3+m)*n+2)#in R^n
        self.parameters.OVIPOSITION=slice((3+m)*n+2,(3+2*m)*n+2)#in R^(mxn)
        self.parameters.vAlpha0=configuration.getArray('biology','alpha0')#constant to be fitted

        #Cordoba
        self.parameters.location={'name':configuration.getString('location','name')}
        self.start_date=configuration.getDate('simulation','start_date')
        self.end_date=configuration.getDate('simulation','end_date')
        self.time_range = np.linspace(0, (self.end_date - self.start_date).days-1, (self.end_date - self.start_date).days )
        initial_condition=configuration.getArray('simulation','initial_condition')
        self.parameters.mBS_l=np.repeat(range(0,m),n).reshape((m,n))#level helper matrix
        E0=np.zeros((m,n))
        E0[0,:]=(initial_condition[0]*self.parameters.vBS_d)
        E0=E0.transpose().reshape((1,m*n)).flatten()
        L0=initial_condition[1]*self.parameters.vBS_d
        P0=initial_condition[2]*self.parameters.vBS_d
        W0=configuration.getArray('breeding_site','initial_water')
        O0=np.zeros((m*n))
        self.parameters.initial_condition=np.concatenate( (E0,L0,P0,initial_condition[-2:],W0,O0) )

        WEATHER_DATA_FILENAME='data/public/'+self.parameters.location['name']+'.csv'
        self.parameters.weather=Weather(WEATHER_DATA_FILENAME,self.start_date,self.end_date)

        self.parameters.mf=self.parameters.weather.getAsLambdaFunction(self.parameters.weather.aps, [0,0,0,0,0,0,1.]* int( (self.end_date - self.start_date).days/7 +1) )

        self.validate()

    def validate(self):
        self.warnings=[]
        mean_temperatures=np.array([self.parameters.weather.T(t) for t in self.time_range])
        lower_bound=mean_temperatures[mean_temperatures<278.]
        upper_bound=mean_temperatures[mean_temperatures>303.]
        if(lower_bound.size>0 or upper_bound.size>0):
            self.warnings.append('Temperature out of model\'s valid range:T<278:%s T>303:%s'%(lower_bound.size,upper_bound.size))

    def save(self,results_filename=None):
        #save results
        if(not results_filename):
            results_filename='data/test/previous_results/'+self.configuration.getString('location','name')+'-'+datetime.datetime.now().strftime('%Y-%m-%d__%H_%M_%S')+'.csv'
        file=open(results_filename,'w')
        daily_Y=utils.getDailyResults(self.time_range,self.Y,self.start_date,self.end_date)
        for d,daily_Y_d in enumerate(daily_Y):
            date_d=self.start_date+datetime.timedelta(days=d)
            file.write(date_d.strftime('%Y-%m-%d')+','+','.join([str(value) for value in daily_Y_d ])+ '\n')
        #save config
        self.configuration.save(results_filename.replace('.csv','.cfg'))

        return results_filename


    def solveEquations(self,equations=equations.diff_eqs,method='rk'):
        time_range=self.time_range
        initial_condition=self.parameters.initial_condition
        Y=None

        if(method=='odeint'):
            Y = spi.odeint(equations,initial_condition,time_range,hmax=1.0,args=(self.parameters,))#the ',' in (parameters,) is very important! '(parameters)' or tuple(parameters) doesn't work#TODO: this is because it calls aps out of it's domain.Find a better way.
        elif(method=='rk'):
            Y=rk.solve(equations,initial_condition,time_range,args=(self.parameters,),steps=20)
        elif(method=='cuda_rk'):
            Y=rk.cuda_solve(equations,initial_condition,time_range,args=(self.parameters,),steps=20)
        elif(method=='rkf'):
            Y=rk.rkf_solve(equations,initial_condition,time_range,args=(self.parameters,))
        elif(method=='dopri'):
            Y=rk.scipy_solve(equations,initial_condition,time_range,'dopri',{'max_step':time_range[1]-time_range[0],'rtol':1e-3, 'atol':1e-6}, args=(self.parameters,))

        self.Y=Y
        return time_range,Y
