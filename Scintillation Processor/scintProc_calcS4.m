function scint = scintProc_calcS4(scint,tm,s4Filter,iCh,iS)
SI_50Hz                     =  scint{iCh}.NBPminusWBP(1:(end-s4Filter.group_delay)) ./ scint{iCh}.NBPminusWBP_detrend((1+s4Filter.group_delay):end);
SI_50Hz(isnan(SI_50Hz))     =  0;
CN0_linear                  = 10^(tm{iCh}.CN0_PLL(iS)/10);
S4_noise                    = (100/CN0_linear)*(1+(500/(19*CN0_linear)));
% S_4
scint{iCh}.S4      = [scint{iCh}.S4,sqrt(( mean(SI_50Hz.^2)- mean(SI_50Hz)^2)/mean(SI_50Hz)^2-S4_noise)];