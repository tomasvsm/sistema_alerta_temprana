#ifndef UtilsH
#define UtilsH

#include <vector>
#include <map>
#include <fstream>
#include <string>
#include <iostream>
#include <cstring>
#include "types.h"
#include "spline.h"

typedef std::vector<int> date;

class Utils{
  public:
    static std::vector<scalar> getValuesFromCsv(const std::string& filename, const std::string& start_date_str, const std::string& end_date_str,int value_column){
      std::ifstream file = std::ifstream(filename,std::ifstream::in);
      if( file.fail() ){
        std::cout << "Couldn't open the file "<<filename<<std::endl;
        exit(0);
      }
      std::map<date,scalar> date_values;
      date start_date=stringToDate(start_date_str);
      date end_date=stringToDate(end_date_str);
  		while(!file.eof()){
          std::string line;
  		    std::getline(file,line);
          if(line=="") continue;
          std::vector<std::string> tokens=parseLine(line,",");
          date current_date=stringToDate(tokens[0]);
          if(start_date<=current_date && current_date<end_date){
            date_values.emplace(current_date,std::stod(tokens[value_column]));
          }
  		}
      file.close();
      std::vector<scalar> values=std::vector<scalar>();
      for(auto it=date_values.begin(); it != date_values.end(); it++) values.push_back(it->second);
      return values;
    }

    static date stringToDate(std::string date_str){
      if(date_str.find("-")==std::string::npos) return date();
      std::vector<std::string> tokens=parseLine(date_str,"-");
      date the_date={std::stoi(tokens[0]),std::stoi(tokens[1]),std::stoi(tokens[2])};//year,month,day
      return the_date;
    }
    static std::vector<std::string> parseLine(std::string strLine, const char* delimiter){
        strLine.erase(std::remove_if(strLine.begin(), strLine.end(), ::isspace), strLine.end());//remove spaces
        const char* constLine=strLine.c_str();
        char* line=strdup(constLine);
        std::vector<std::string> tokens;
        char* token=strtok(line,delimiter);
        tokens.push_back(token);
        while( (token=strtok(NULL,delimiter)) ){
            tokens.push_back(token);
        }

        return tokens;
    }

    static std::vector<scalar> getAverageTemperaturesFromCsv(const std::string& filename, const std::string& start_date, const std::string& end_date){///#in Kelvin#TODO:change name to meanTemperature
        std::vector<scalar> temperatures=getValuesFromCsv(filename,start_date,end_date,2);
        for(unsigned int i=0;i<temperatures.size();i++) temperatures[i]+=273.15;
        return temperatures;
    }

    static std::vector<scalar> getPrecipitationsFromCsv(const std::string& filename, const std::string& start_date, const std::string& end_date){//in mm
        return getValuesFromCsv(filename,start_date,end_date,4);
    }

    static std::vector<scalar> getRelativeHumidityFromCsv(const std::string& filename, const std::string& start_date, const std::string& end_date){//in percentage
        return getValuesFromCsv(filename,start_date,end_date,5);
    }

    static unsigned int getDaysFromCsv(const std::string& filename, const std::string& start_date, const std::string& end_date){//convenience method to get amount of days between two dates
        return getValuesFromCsv(filename,start_date,end_date,0).size();
    }

    //Convenience method
    static tk::spline getSpline(std::vector<scalar> X,std::vector<scalar> Y){
        tk::spline s;
        s.set_boundary(tk::spline::second_deriv, 0.0,tk::spline::second_deriv,0.0,false);//This is the default, just calling to avoid warning.
        s.set_points(X,Y);    // currently it is required that X is already sorted
        return s;
    }

    static tensor sumAxis0(const matrix& M){
        return M.colwise().sum();
    }

    static tensor minimum(const tensor& tensor1,const tensor& tensor2){
        return tensor1.min(tensor2);
    }
    static tensor maximum(const tensor& tensor1,const tensor& tensor2){
        return tensor1.max(tensor2);
    }
};

#endif
