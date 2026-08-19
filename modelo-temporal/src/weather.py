import math
import utils
from scipy import interpolate

class Weather:
    '''
    Convenience method
    Let  g=getAsLambdaFunction(aps2,precipitations)
    calling g(t) would be as calling aps2(precipitations,t)
    '''
    def getAsLambdaFunction(self,f,values):
        return lambda t: f(values,t)

    #Area preserving Sin
    def aps(self,values,t):
        return values[int(t)] * (math.sin(2.*math.pi*t + 3.*math.pi/2.) +1.)
    
    #will be defined in init
    p = None
    T = None
    RH = None
    def __init__(self, filename,start_date,end_date):
        precipitations = utils.getPrecipitationsFromCsv(filename,start_date,end_date)
        self.p = self.getAsLambdaFunction(self.aps,precipitations)
        self.T = interpolate.InterpolatedUnivariateSpline(range(0,(end_date - start_date).days),utils.getAverageTemperaturesFromCsv(filename,start_date,end_date))
        self.RH = interpolate.InterpolatedUnivariateSpline(range(0,(end_date - start_date).days),utils.getRelativeHumidityFromCsv(filename,start_date,end_date))
