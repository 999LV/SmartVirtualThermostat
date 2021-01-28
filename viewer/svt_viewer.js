
var hardwareReq = "/json.htm?type=hardware";
var tempReq = "/json.htm?type=graph&sensor=temp&range=day&idx=";
var switchReq = "/json.htm?type=lightlog&idx=";
var devicesReq = "/json.htm?type=devices";

class Thermostat {
	constructor() {
		this.name = "";
		this.tempIn = undefined;
		this.tempOut = undefined;
		this.heater=undefined;
	}
}
class Heater {
	constructor() {
		this.historic=undefined;
		this.isDimmer=false;
	}
}
Date.prototype.yyyymmdd_hhmm = function() {
		var mm = this.getMonth() + 1; // getMonth() is zero-based
		var dd = this.getDate();
		var hh = this.getHours();
		var mm = this.getMinutes();

		return [this.getFullYear(),
			(mm>9 ? '' : '0') + mm,
			(dd>9 ? '' : '0') + dd,
			'_',
			(hh>9 ? '' : '0') + hh,
			(mm>9 ? '' : '0') + mm
			].join('');
	};
function getRequestParam(name){
	var req = location.search;
	if(req.length == 0) {
		req = location.href;
	}
	if(name=(new RegExp('[?&]'+encodeURIComponent(name)+'=([^&]*)')).exec(req))
		return decodeURIComponent(name[1]);
}
function getThermostats() {
	var request=proto+address + ":" + port.toString() + hardwareReq;
	var thermostats = [];
	$.ajax({url: request,
		async: false,
		success: function(result){
			for(i=0;i<result.result.length;i++) {
				if(result.result[i].Extra == "SVT") {
					var thermostat = new Thermostat();
					var heats=[];
					var tempOut = undefined;
					var tempInIdxs = result.result[i].Mode1.split(',');
					var tempOutIdx = result.result[i].Mode2;
					var heatIdxs = result.result[i].Mode3.split(',');
					var name = result.result[i].Name;
					thermostat.name = name;
					thermostat.id = result.result[i].idx;
					var minDate = 0;
					var request=proto+address + ":" + port.toString() + tempReq + tempInIdxs[0];
					$.ajax({url: request,
						async: false,
						success: function(resultTemp){
							//console.log(resultTemp);
							thermostat.tempIn = resultTemp.result;
							minDate = Math.min.apply(null,
								resultTemp.result.map(function (item) {
									return new Date(item.d);
								})
							);
						}
					});
					if(tempOutIdx != undefined && tempOutIdx >= 0) {
						request=proto+address + ":" + port.toString() + tempReq + tempOutIdx;
						$.ajax({url: request,
							async: false,
							success: function(resultTemp){
								//console.log(resultTemp);
								thermostat.tempOut = resultTemp.result;
								var minDate2 = Math.min.apply(null, 
									resultTemp.result.map(function (item) {
										return new Date(item.d);
									})
								);
								minDate = Math.min(minDate, minDate2);
							}
						});
					}
					request=proto+address + ":" + port.toString() + switchReq + heatIdxs[0];
					$.ajax({url: request,
						async: false,
						success: function(resultTemp){
							//console.log(resultTemp);
							thermostat.heater = new Heater();
							//thermostat.heater.isDimmer = resultTemp.HaveDimmer; // not working, HaveDimmer always true
							thermostat.heater.historic = resultTemp.result.filter(item => new Date(item.Date).getTime() >= minDate).sort((a,b)=>new Date(a.Date).getTime()>new Date(b.Date).getTime());
						}
					});
					request=proto+address + ":" + port.toString() + devicesReq;
					$.ajax({url: request,
						async: false,
						success: function(resultTemp){
							for(var j=0;j<resultTemp.result.length;j++) {
								if(thermostat.id == resultTemp.result[j].HardwareID && resultTemp.result[j].Unit == 4) {
									request=proto+address + ":" + port.toString() + tempReq + resultTemp.result[j].idx;
									$.ajax({url: request,
										async: false,
										success: function(resultTemp2){
											//console.log(resultTemp2);
											thermostat.setpoint = resultTemp2.result;
										}
									});
								}
								if(heatIdxs[0] == resultTemp.result[j].idx) {
									thermostat.heater.isDimmer = resultTemp.result[j].SwitchType == "Dimmer";
								}
							}
						}
					});
					thermostats.push(thermostat);
				}
			}
			//console.log(thermostats);
		}
	});
	return thermostats;
}
