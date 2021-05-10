# HydDown hydrogen/other gas depressurisation
# Copyright (c) 2021 Anders Andreasen
# Published under an MIT license

import math
import numpy as np
import pandas as pd
from scipy.optimize import fmin
from scipy.optimize import minimize
from CoolProp.CoolProp import PropsSI
from hyddown import transport as tp
from hyddown import validator 
from hyddown import fire


class HydDown:
    def __init__(self, input):
        self.input = input
        self.verbose = 0
        self.isrun = False
        self.validate_input()
        self.read_input()
        self.initialize()


    def validate_input(self):
        valid = validator.validation(self.input)
        if valid is False:
            raise ValueError("Error in input file")


    def read_input(self):
        self.length = self.input["vessel"]["length"]
        self.diameter = self.input["vessel"]["diameter"]

        self.p0 = self.input["initial"]["pressure"]
        self.T0 = self.input["initial"]["temperature"]
        self.species = "HEOS::" + self.input["initial"]["fluid"]

        self.tstep = self.input["calculation"]["time_step"]
        self.time_tot = self.input["calculation"]["end_time"]
        self.method = self.input["calculation"]["type"]

        # Reading valve specific data
        if (
            self.input["valve"]["type"] == "orifice"
            or self.input["valve"]["type"] == "psv"
        ):
            self.p_back = self.input["valve"]["back_pressure"]
            self.D_orifice = self.input["valve"]["diameter"]
            self.CD = self.input["valve"]["discharge_coef"]
            if self.input["valve"]["type"] == "psv":
                self.Pset = self.input["valve"]["set_pressure"]
                self.blowdown = self.input["valve"]["blowdown"]
                self.psv_state = "closed"
        elif self.input["valve"]["type"] == "controlvalve":
            self.p_back = self.input["valve"]["back_pressure"]
            self.Cv = self.input["valve"]["Cv"]
            if "xT" in self.input["valve"]:
                self.xT = self.input["valve"]["xT"]
            if "Fp" in self.input["valve"]:
                self.Fp = self.input["valve"]["Fp"]
        elif (
            self.input["valve"]["type"] == "mdot"
            and self.input["valve"]["flow"] == "filling"
        ):
            self.p_back = self.input["valve"]["back_pressure"]

        # valve type
        # - constant_mass
        # - functional mass flow
        self.thickness = 0
        # Reading heat transfer related data/information
        if "heat_transfer" in self.input:
            self.heat_method = self.input["heat_transfer"]["type"]
            if self.heat_method == "specified_h" or self.heat_method == "specified_U":
                self.Tamb = self.input["heat_transfer"]["temp_ambient"]
            if self.heat_method == "specified_U":
                self.Ufix = self.input["heat_transfer"]["U_fix"]
            if self.heat_method == "specified_Q":
                self.Qfix = self.input["heat_transfer"]["Q_fix"]
            if self.heat_method == "specified_h":
                self.vessel_cp = self.input["vessel"]["heat_capacity"]
                self.vessel_density = self.input["vessel"]["density"]
                self.vessel_orientation = self.input["vessel"]["orientation"]
                self.thickness = self.input["vessel"]["thickness"]
                self.h_out = self.input["heat_transfer"]["h_outer"]
                self.h_in = self.input["heat_transfer"]["h_inner"]
                if self.input["valve"]["flow"] == "filling":
                    self.D_throat = self.input["heat_transfer"]["D_throat"]
            if self.heat_method == "s-b":
                self.fire_type = self.input["heat_transfer"]["fire"]
                self.h_in = "calc"
                self.vessel_cp = self.input["vessel"]["heat_capacity"]
                self.vessel_density = self.input["vessel"]["density"]
                self.vessel_orientation = self.input["vessel"]["orientation"]
                self.thickness = self.input["vessel"]["thickness"]
                if self.input["valve"]["flow"] == "filling":
                    raise ValueError("Filling and Fire heat load not implemented")
                

    def initialize(self):
        self.vol = self.diameter ** 2 / 4 * math.pi * self.length  # m3
        self.vol_tot = (
            (self.diameter + 2 * self.thickness) ** 2
            / 4
            * math.pi
            * (self.length + 2 * self.thickness)
        )  # m3
        self.vol_solid = self.vol_tot - self.vol
        self.surf_area_outer = (
            self.diameter + 2 * self.thickness
        ) ** 2 / 4 * math.pi * 2 + (self.diameter + 2 * self.thickness) * math.pi * (
            self.length + 2 * self.thickness
        )
        self.surf_area_inner = (self.diameter) ** 2 / 4 * math.pi * 2 + (
            self.diameter
        ) * math.pi * self.length

        # data storage
        data_len = int(self.time_tot / self.tstep)
        self.rho = np.zeros(data_len)
        self.T_fluid = np.zeros(data_len)
        self.T_vent = np.zeros(data_len)
        self.T_vessel = np.zeros(data_len)
        self.Q_outer = np.zeros(data_len)
        self.Q_inner = np.zeros(data_len)
        self.h_inside = np.zeros(data_len)
        self.T_vent = np.zeros(data_len)
        self.H_mass = np.zeros(data_len)
        self.S_mass = np.zeros(data_len)
        self.U_mass = np.zeros(data_len)
        self.U_tot = np.zeros(data_len)
        self.P = np.zeros(data_len)
        self.mass_fluid = np.zeros(data_len)
        self.mass_rate = np.zeros(data_len)
        self.time_array = np.zeros(data_len)

        self.rho0 = PropsSI("D", "T", self.T0, "P", self.p0, self.species)
        self.m0 = self.rho0 * self.vol


    def UDproblem(self, U, rho, Pguess, Tguess):
        if "&" in self.species:                     
            res_fun = lambda x: ((U-PropsSI("U","P",x[0],"T",x[1],self.species))/U)**2 + ((rho-PropsSI("D","P",x[0],"T",x[1],self.species))/rho)**2
            x0=[Pguess,Tguess]
            bounds=[(Pguess*0.95,Pguess+1e5),(Tguess-1,Tguess+1)]
            res=minimize(res_fun,x0,method='Nelder-Mead')
            P1 = res.x[0]
            T1 = res.x[1]
        else:
            P1 = PropsSI(
                "P", "D", rho, "U", U, self.species
                )
            T1 = PropsSI(
                "T", "D", rho, "U", U, self.species
               )

        return P1, T1


    def run(self):
        # Inititialise
        input = self.input
        self.rho[0] = self.rho0
        self.T_fluid[0] = self.T0
        self.T_vessel[0] = self.T0
        if self.input["valve"]["flow"] == "discharge": self.T_vent[0] = self.T0
        self.H_mass[0] = PropsSI("H", "T", self.T0, "P", self.p0, self.species)
        self.S_mass[0] = PropsSI("S", "T", self.T0, "P", self.p0, self.species)
        self.U_mass[0] = PropsSI("U", "T", self.T0, "P", self.p0, self.species)
        self.U_tot[0] = PropsSI("U", "T", self.T0, "P", self.p0, self.species) * self.m0
        self.P[0] = self.p0
        self.mass_fluid[0] = self.m0
        cpcv = PropsSI("CP0MOLAR", "T", self.T0, "P", self.p0, self.species) / PropsSI(
            "CVMOLAR", "T", self.T0, "P", self.p0, self.species
        )
        massflow_stop_switch = 0 

        if input["valve"]["type"] == "orifice":
            if input["valve"]["flow"] == "filling":
                k = PropsSI(
                    "CP0MOLAR", "T", self.T0, "P", self.p_back, self.species
                ) / PropsSI("CVMOLAR", "T", self.T0, "P", self.p_back, self.species)
                self.mass_rate[0] = -tp.gas_release_rate(
                    self.p_back,
                    self.p0,
                    PropsSI("D", "T", self.T0, "P", self.p_back, self.species),
                    k,
                    self.CD,
                    self.D_orifice ** 2 / 4 * math.pi,
                )
            else:
                self.mass_rate[0] = tp.gas_release_rate(
                    self.p0,
                    self.p_back,
                    self.rho0,
                    cpcv,
                    self.CD,
                    self.D_orifice ** 2 / 4 * math.pi,
                )
        elif input["valve"]["type"] == "mdot":
            if "mdot" in input["valve"].keys() and "time" in input["valve"].keys():
                mdot = np.asarray(input["valve"]["mdot"])
                time = np.asarray(input["valve"]["time"])
                max_i = int(time[-1] / self.tstep)
                interp_time = np.linspace(
                    0,
                    self.tstep * len(self.time_array),
                    len(self.time_array),
                    endpoint=False,
                )[:max_i]
                self.mass_rate[:max_i] = np.interp(interp_time, time, mdot)
                if input["valve"]["flow"] == "filling":
                    self.mass_rate *= -1

            else:
                if input["valve"]["flow"] == "filling":
                    self.mass_rate[:] = -input["valve"]["mass_flow"]
                else:
                    self.mass_rate[:] = input["valve"]["mass_flow"]

        elif input["valve"]["type"] == "controlvalve":
            if input["valve"]["flow"] == "filling":
                Z = PropsSI("Z", "T", self.T0, "P", self.p_back, self.species)
                MW = PropsSI("M", "T", self.T0, "P", self.p_back, self.species)
                k = PropsSI(
                    "CP0MOLAR", "T", self.T0, "P", self.p_back, self.species
                ) / PropsSI("CVMOLAR", "T", self.T0, "P", self.p_back, self.species)
                self.mass_rate[0] = -tp.control_valve(
                    self.p_back, self.p0, self.T0, Z, MW, k, self.Cv
                )
            else:
                Z = PropsSI("Z", "T", self.T0, "P", self.p0, self.species)
                MW = PropsSI("M", "T", self.T0, "P", self.p0, self.species)
                k = PropsSI(
                    "CP0MOLAR", "T", self.T0, "P", self.p0, self.species
                ) / PropsSI("CVMOLAR", "T", self.T0, "P", self.p0, self.species)
                self.mass_rate[0] = tp.control_valve(
                    self.p0, self.p_back, self.T0, Z, MW, k, self.Cv
                )
        elif input["valve"]["type"] == "psv":
            if input["valve"]["flow"] == "filling":
                raise ValueError(
                    "Unsupported valve: ",
                    input["valve"]["type"],
                    " for vessel filling.",
                )
            self.mass_rate[0] = tp.relief_valve(
                self.p0,
                self.p_back,
                self.Pset,
                self.blowdown,
                cpcv,
                self.CD,
                self.T0,
                PropsSI('Z', 'T', self.T0, 'P', self.p0, self.species),
                PropsSI('M', self.species),
                self.D_orifice ** 2 / 4 * math.pi,
            )

        self.time_array[0] = 0
        # Run actual integration
        for i in range(1, len(self.time_array)):
            self.time_array[i] = self.time_array[i - 1] + self.tstep
            self.mass_fluid[i] = (
                self.mass_fluid[i - 1] - self.mass_rate[i - 1] * self.tstep
            )
             
            self.rho[i] = self.mass_fluid[i] / self.vol

            if self.method == "isenthalpic":
                self.T_fluid[i] = PropsSI(
                    "T", "D", self.rho[i], "H", self.H_mass[i - 1], self.species
                )
                self.P[i] = PropsSI(
                    "P", "D", self.rho[i], "H", self.H_mass[i - 1], self.species
                )
            elif self.method == "isentropic":
                self.T_fluid[i] = PropsSI(
                    "T", "D", self.rho[i], "S", self.S_mass[i - 1], self.species
                )
                self.P[i] = PropsSI(
                    "P", "D", self.rho[i], "S", self.S_mass[i - 1], self.species
                )
            elif self.method == "isothermal":
                self.T_fluid[i] = self.T0
                self.P[i] = PropsSI("P", "D", self.rho[i], "T", self.T0, self.species)
            elif self.method == "constantU":
                self.T_fluid[i] = PropsSI(
                    "T", "D", self.rho[i], "U", self.U_mass[i - 1], self.species
                )
                self.P[i] = PropsSI(
                    "P", "D", self.rho[i], "U", self.U_mass[i - 1], self.species
                )
            elif self.method == "energybalance":
                if self.heat_method == "specified_h" or self.heat_method == "detailed":
                    if self.h_in == "calc":
                        if self.vessel_orientation == "horizontal":
                            L = self.diameter
                        else:
                            L = self.length
                        if input["valve"]["flow"] == "filling":
                            hi = tp.h_inner_mixed(
                                L,
                                self.T_fluid[i - 1],
                                self.T_vessel[i - 1],
                                self.P[i - 1],
                                self.species,
                                self.mass_rate[i - 1],
                                (self.D_throat) / 1,
                            )
                        else:
                            hi = tp.h_inner(
                                L,
                                self.T_fluid[i - 1],
                                self.T_vessel[i - 1],
                                self.P[i - 1],
                                self.species,
                            )
                    else:
                        hi = self.h_in
                    self.h_inside[i] = hi
                    self.Q_inner[i] = (
                        self.surf_area_inner
                        * hi
                        * (self.T_vessel[i - 1] - self.T_fluid[i - 1])
                    )
                    self.Q_outer[i] = (
                        self.surf_area_outer
                        * self.h_out
                        * (self.Tamb - self.T_vessel[i - 1])
                    )
                    self.T_vessel[i] = self.T_vessel[i - 1] + (
                        self.Q_outer[i] - self.Q_inner[i]
                    ) * self.tstep / (
                        self.vessel_cp * self.vessel_density * self.vol_solid
                    )
                elif self.heat_method == "s-b":
                    if self.vessel_orientation == "horizontal":
                        L = self.diameter
                    else:
                        L = self.length
                    hi = tp.h_inner(
                        L,
                        self.T_fluid[i - 1],
                        self.T_vessel[i - 1],
                        self.P[i - 1],
                        self.species,
                    )
                    self.h_inside[i] = hi
                    self.Q_inner[i] = (
                        self.surf_area_inner
                        * hi
                        * (self.T_vessel[i - 1] - self.T_fluid[i - 1])
                    )
                    self.Q_outer[i] = fire.sb_fire(self.T_vessel[i-1], self.fire_type) * self.surf_area_outer
                    self.T_vessel[i] = self.T_vessel[i - 1] + (
                        self.Q_outer[i] - self.Q_inner[i]
                    ) * self.tstep / (
                        self.vessel_cp * self.vessel_density * self.vol_solid
                    )
                elif self.heat_method == "specified_U":
                    self.Q_inner[i] = (
                        self.surf_area_outer
                        * self.Ufix
                        * (self.Tamb - self.T_fluid[i - 1])
                    )
                    self.T_vessel[i] = self.T_vessel[0]
                elif self.heat_method == "specified_Q":
                    self.Q_inner[i] = self.Qfix
                    self.T_vessel[i] = self.T_vessel[0]
                else:
                    self.Q_inner[i] = 0.0
                    self.T_vessel[i] = self.T_vessel[0]

                NMOL = self.mass_fluid[i - 1] / PropsSI("M", self.species)
                NMOL_ADD = (self.mass_fluid[i] - self.mass_fluid[i - 1]) / PropsSI(
                    "M", self.species
                )
                if input["valve"]["flow"] == "filling":
                    T_used = self.T0
                    P_used = self.p_back
                else:
                    T_used = self.T_fluid[i - 1]
                    P_used = self.P[i - 1]

                # New
                U_start = self.U_mass[i - 1] * self.mass_fluid[i - 1]
                x = 1 - math.exp(-1 * self.time_array[i]) ** 0.66
                h_in = x * PropsSI("H", "T", T_used, "P", P_used, self.species) + (
                    1 - x
                ) * PropsSI("U", "T", T_used, "P", P_used, self.species)
                P2 = self.P[i - 1]
                if i > 1:
                    P1 = self.P[i - 2]
                else:
                    P1 = self.P[i - 1]
                U_end = (
                    U_start
                    - self.tstep * self.mass_rate[i - 1] * h_in
                    + self.tstep * self.Q_inner[i]
                )  
                self.U_mass[i] = U_end / self.mass_fluid[i]
                #print("Iteration: ",i," of ",len(self.P))
                P1, T1 = self.UDproblem(U_end/ self.mass_fluid[i],self.rho[i],self.P[i-1],self.T_fluid[i-1])

                self.P[i] = P1
                self.T_fluid[i] = T1


            else:
                raise NameError("Unknown calculation method: " + self.method)

            self.H_mass[i] = PropsSI(
                "H", "T", self.T_fluid[i], "P", self.P[i], self.species
            )
            self.S_mass[i] = PropsSI(
                "S", "T", self.T_fluid[i], "P", self.P[i], self.species
            )
            self.U_mass[i] = PropsSI(
                "U", "T", self.T_fluid[i], "P", self.P[i], self.species
            )  

            if self.input["valve"]["flow"] == "discharge":
                self.T_vent[i] = PropsSI("T", "H", self.H_mass[i], "P", self.p_back, self.species)

            cpcv = PropsSI(
                "CP0MOLAR", "T", self.T_fluid[i], "P", self.P[i], self.species
            ) / PropsSI("CVMOLAR", "T", self.T_fluid[i], "P", self.P[i], self.species)

            if input["valve"]["type"] == "orifice":
                if input["valve"]["flow"] == "filling":
                    k = PropsSI(
                        "CP0MOLAR", "T", self.T0, "P", self.p_back, self.species
                    ) / PropsSI("CVMOLAR", "T", self.T0, "P", self.p_back, self.species)
                    self.mass_rate[i] = -tp.gas_release_rate(
                        self.p_back,
                        self.P[i],
                        PropsSI("D", "T", self.T0, "P", self.p_back, self.species),
                        k,
                        self.CD,
                        self.D_orifice ** 2 / 4 * math.pi,
                    )
                else:
                    self.mass_rate[i] = tp.gas_release_rate(
                        self.P[i],
                        self.p_back,
                        self.rho[i],
                        cpcv,
                        self.CD,
                        self.D_orifice ** 2 / 4 * math.pi,
                    )
            elif input["valve"]["type"] == "controlvalve":
                if input["valve"]["flow"] == "filling":
                    Z = PropsSI("Z", "T", self.T0, "P", self.p_back, self.species)
                    MW = PropsSI("M", "T", self.T0, "P", self.p_back, self.species)
                    k = PropsSI(
                        "CP0MOLAR", "T", self.T0, "P", self.p_back, self.species
                    ) / PropsSI("CVMOLAR", "T", self.T0, "P", self.p_back, self.species)
                    self.mass_rate[i] = -tp.control_valve(
                        self.p_back, self.P[i], self.T0, Z, MW, k, self.Cv
                    )
                else:
                    Z = PropsSI("Z", "T", self.T_fluid[i], "P", self.P[i], self.species)
                    MW = PropsSI(
                        "M", "T", self.T_fluid[i], "P", self.P[i], self.species
                    )
                    self.mass_rate[i] = tp.control_valve(
                        self.P[i], self.p_back, self.T_fluid[i], Z, MW, cpcv, self.Cv
                    )
            elif input["valve"]["type"] == "psv":
                self.mass_rate[i] = tp.relief_valve(
                    self.P[i],
                    self.p_back,
                    self.Pset,
                    self.blowdown,
                    cpcv,
                    self.CD,
                    self.T_fluid[i],
                    PropsSI('Z', 'T', self.T_fluid[i], 'P', self.P[i], self.species),
                    PropsSI('M', self.species),
                    self.D_orifice ** 2 / 4 * math.pi,
                )
            if 'end_pressure' in self.input['valve'] and self.P[i] > self.input['valve']['end_pressure']:
                massflow_stop_switch = 1
            if massflow_stop_switch:
                self.mass_rate[i]=0
        self.isrun = True

    def get_dataframe(self):
        if self.isrun == True:
            df=pd.DataFrame(self.time_array,columns=['Time (s)'])
            
            df.insert(1,"Pressure (bar)", self.P/1e5, True)
            df.insert(2,"Fluid temperature (oC)", self.T_fluid - 273.15, True)
            df.insert(3,"Wall temperature  (oC)", self.T_vessel - 273.15, True)
            df.insert(4,"Vent temperature  (oC)", self.T_vessel - 273.15, True)
            df.insert(5, "Fluid enthalpy (J/kg)", self.H_mass, True)
            df.insert(6, "Fluid entropy (J/kg K)", self.S_mass, True)
            df.insert(7, "Fluid internal energy (J/kg)", self.U_mass, True)
            df.insert(8, "Discharge mass rate (kg/s)", self.mass_rate, True)
            df.insert(9, "Fluid mass (kg)", self.mass_fluid, True)
            df.insert(10, "Fluid density (kg/m3)", self.rho, True)
            df.insert(11, "Inner heat transfer coefficient (W/m2 K)", self.h_inside, True)
            df.insert(12, "Internal heat flux (W/m2)", self.Q_inner/self.surf_area_inner, True)
            df.insert(13, "External heat flux (W/m2)", self.Q_outer/self.surf_area_outer, True)
            
        return df

    def plot(self,filename=None):
        import pylab as plt

        if filename != None:
            plt.figure(figsize=(12,7),dpi=300)
        else:
            plt.figure()
            
        plt.subplot(221)
        plt.plot(self.time_array , self.T_fluid - 273.15, "b", label="Fluid")
        plt.plot(self.time_array , self.T_vessel - 273.15, "g", label="Vessel")
        if self.input["valve"]["flow"] == "discharge":
            plt.plot(self.time_array , self.T_vent - 273.15, "r", label="Vent")
        if "validation" in self.input:
            if "temperature" in self.input["validation"]:
                temp = self.input["validation"]["temperature"]
                if "gas_mean" in temp:
                    plt.plot(
                        np.asarray(temp["gas_mean"]["time"]),
                        np.asarray(temp["gas_mean"]["temp"]) - 273.15,
                        "b:",
                        label="Gas mean",
                    )
                if "gas_high" in temp:
                    plt.plot(
                        np.asarray(temp["gas_high"]["time"]),
                        np.asarray(temp["gas_high"]["temp"]) - 273.15,
                        "b-.",
                        label="Gas high",
                    )
                if "gas_low" in temp:
                    plt.plot(
                        np.asarray(temp["gas_low"]["time"]),
                        np.asarray(temp["gas_low"]["temp"]) - 273.15,
                        "b--",
                        label="Gas low",
                    )
                if "wall_mean" in temp:
                    plt.plot(
                        np.asarray(temp["wall_mean"]["time"]),
                        np.asarray(temp["wall_mean"]["temp"]) - 273.15,
                        "g:",
                        label="Wall mean",
                    )
                if "wall_high" in temp:
                    plt.plot(
                        np.asarray(temp["wall_high"]["time"]),
                            np.asarray(temp["wall_high"]["temp"]) - 273.15,
                        "g-.",
                        label="Wall high",
                    )
                if "wall_low" in temp:
                    plt.plot(
                        np.asarray(temp["wall_low"]["time"]),
                        np.asarray(temp["wall_low"]["temp"]) - 273.15,
                        "g--",
                        label="Wall low",
                    )
        plt.legend(loc="best")
        plt.xlabel("Time (seconds)")
        plt.ylabel("Temperature ($^\circ$C)")

        plt.subplot(222)
        plt.plot(self.time_array , self.P / 1e5, "b", label="Calculated")
        if "validation" in self.input:
            if "pressure" in self.input["validation"]:
                plt.plot(
                    np.asarray(self.input["validation"]["pressure"]["time"]),
                    self.input["validation"]["pressure"]["pres"],
                    "ko",
                    label="Experimental",
                )
        plt.legend(loc="best")
        plt.xlabel("Time (seconds)")
        plt.ylabel("Pressure (bar)")

        plt.subplot(223)
        plt.plot(self.time_array , self.H_mass, "b", label="H (J/kg)")
        plt.plot(self.time_array , self.U_mass, "g", label="U (J/kg)")
        plt.plot(self.time_array , self.S_mass * 100, "r", label="S*100 (J/kg K)")
        plt.legend(loc="best")
        plt.xlabel("Time (seconds)")
        plt.ylabel("Enthalpy/Internal Energy/Entropy")

        plt.subplot(224)
        plt.plot(self.time_array , self.mass_rate, "b", label="m_dot")
        plt.xlabel("Time (seconds)")
        plt.ylabel("Vent rate (kg/s)")

        if filename != None:
            plt.savefig(filename)
        elif self.verbose:
            plt.show()


    def __str__(self):
        return "HydDown vessel filling/depressurization class"
        
    def generate_report(self):
        report = {}

        report["start_time"] = self.time_array[0]
        report["end_time"] = self.time_array[-1]

        # Pressure
        report["max_pressure"] = max(self.P)
        report["time_max_pressure"] = self.time_array[np.argmax(self.P)]
        report["min_pressure"] = min(self.P)
        report["time_min_pressure"] = self.time_array[np.argmin(self.P)]

        # Temperatures
        report["max_fluid_temp"] = max(self.T_fluid)
        report["time_max_fluid_temp"] = self.time_array[np.argmax(self.T_fluid)]
        report["min_fluid_temp"] = min(self.T_fluid)
        report["time_min_fluid_temp"] = self.time_array[np.argmin(self.T_fluid)]

        report["max_wall_temp"] = max(self.T_vessel)
        report["time_max_wall_temp"] = self.time_array[np.argmax(self.T_vessel)]
        report["min_wall_temp"] = min(self.T_vessel)
        report["time_min_wall_temp"] = self.time_array[np.argmin(self.T_vessel)]

        # Mass flows and inventory
        report["max_mass_rate"] = max(self.mass_rate)
        report["initial_mass"] = self.mass_fluid[0]
        report["final_mass"] = self.mass_fluid[-1]
        report["volume"] = self.vol

        report["max_Q_inside"] = max(self.Q_inner)
        report["time_max_Q_inside"] = self.time_array[np.argmax(self.Q_inner)]
        report["max_heat_flux_inside"] = max(self.Q_inner / self.surf_area_inner)

        report["max_Q_outside"] = max(self.Q_outer)
        report["time_max_Q_outside"] = self.time_array[np.argmax(self.Q_outer)]
        report["max_heat_flux_outside"] = max(self.Q_outer / self.surf_area_outer)

        self.report = report
