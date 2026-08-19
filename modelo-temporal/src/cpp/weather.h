//https://kluge.in-chemnitz.de/opensource/spline/
#ifndef WeatherH
#define WeatherH

#include<vector>
#include<iostream>
#include <functional>
#include <numeric>
#include "utils.h"

class Weather
{

public:
    std::function<scalar(scalar)> p;
    std::function<scalar(scalar)> T;
    std::function<scalar(scalar)> RH;

    Weather(){};

	Weather(std::string filename, std::string start_date,std::string end_date){
        std::vector<scalar> precipitations=Utils::getPrecipitationsFromCsv(filename,start_date,end_date);
        std::vector<scalar> temperatures=Utils::getAverageTemperaturesFromCsv(filename,start_date,end_date);
        std::vector<scalar> relative_humidities=Utils::getRelativeHumidityFromCsv(filename,start_date,end_date);
		std::vector<scalar> days=std::vector<scalar>(temperatures.size());
		std::iota(days.begin(),days.end(),0);

        this->p = [precipitations](scalar t) { return precipitations[int(t)]* (sin(2.*M_PI*t + 3.*M_PI/2.) +1.); };
        this->T = Utils::getSpline(days,temperatures);
        this->RH = Utils::getSpline(days,relative_humidities);

	}


};

#endif
