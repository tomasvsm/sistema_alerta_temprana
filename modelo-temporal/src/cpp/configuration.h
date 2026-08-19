#ifndef ConfigurationH
#define ConfigurationH

#include <fstream>
#include "types.h"
#include "utils.h"
class Configuration
{
    public:
    std::string filename;
    explicit Configuration(const std::string& filename):filename(filename){}//explicit is just to avoid a cpp check warning

    std::string get(const std::string& section, const std::string& option){
        std::ifstream file = std::ifstream(this->filename,std::ifstream::in);
        bool in_section=false;
        while(!file.eof()){
          std::string line;
          std::getline(file,line);
          if(line=="") continue;
          if(line=="["+section+"]") in_section=true;
          if(in_section){
              std::vector<std::string> tokens=Utils::parseLine(line,"=");
              if(tokens[0]==option){
                  file.close();
                  return tokens[1];
              }
          }
        }
        std::cout << "Warning: option not found "<< section<<" "<<option<<std::endl;
        file.close();
        return "";
    }

    scalar getScalar(const std::string& section, const std::string& option){
        std::string value=get(section,option);
        return std::stod(value);//TODO: here we have an implicit double->scalar conversion
    }
    tensor getTensor(const std::string& section, const std::string& option){
        std::string value=get(section,option);
        std::vector<std::string> tokens=Utils::parseLine(value,",");
        std::vector<scalar> values=std::vector<scalar>();
        for(std::string token:tokens) values.push_back(std::stod(token));//TODO: here we have an implicit double->scalar conversion
        tensor t=tensor(values.size());
        for(unsigned int i=0;i<values.size();i++) t(i)=values[i];
        return t;
    }

};
#endif
