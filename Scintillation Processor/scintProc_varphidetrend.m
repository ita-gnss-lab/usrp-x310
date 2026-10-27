        function scint = scintProc_varphidetrend(iCh,iS,scint,varphiFilter,param,tm)
            if param.iIntPeriod <param.T_average_varphi
                if tm{iCh}.flPessimPLL(iS)
                    scint{iCh}.phi                 = [scint{iCh}.phi; tm{iCh}.phNCOScint(iS)-param.Phi_ref];
                else
                    scint{iCh}.phi                 = [scint{iCh}.phi; scint{iCh}.phi(end)];
                end
            elseif param.iIntPeriod == param.T_average_varphi
                if tm{iCh}.flPessimPLL(iS)
                    scint{iCh}.phi                 = [scint{iCh}.phi; tm{iCh}.phNCOScint(iS)-param.Phi_ref];
                else
                    scint{iCh}.phi                 = [scint{iCh}.phi; scint{iCh}.phi(end)];
                end
                X                                           = linspace(param.iIntPeriod - param.T_average_varphi ,param.iIntPeriod,length(scint{iCh}.phi));
                Poly_Coef                                   = polyfit(X(:),scint{iCh}.phi(:),3);
                Phi_res                                     = scint{iCh}.phi(:)-(Poly_Coef(1)*X.^3+Poly_Coef(2)*X.^2+Poly_Coef(3)*X+Poly_Coef(4)).';
                for i= 1:length(Phi_res)-6
                    scint{iCh}.phi_filtered        = [scint{iCh}.phi_filtered;...
                        (varphiFilter.b_coef*Phi_res(i:(i+6))-...
                        varphiFilter.a_coef(1:(end-1))*scint{iCh}.phi_filtered((end-length( varphiFilter.a_coef)+2):(end)))/...
                        varphiFilter.a_coef(end)];
                end
                
            elseif param.iIntPeriod > param.T_average_varphi
                if tm{iCh}.flPessimPLL(iS)
                    scint{iCh}.phi                 = [scint{iCh}.phi(2:end); tm{iCh}.phNCOScint(iS)-param.Phi_ref];
                else
                    scint{iCh}.phi                 = [scint{iCh}.phi(2:end); scint{iCh}.phi(end)];
                end
                X                                           = linspace(param.iIntPeriod - param.T_average_varphi ,param.iIntPeriod,length(scint{iCh}.phi));
                Poly_Coef                                   = polyfit(X(:),scint{iCh}.phi(:),3);
                Phi_res                                     = scint{iCh}.phi(:)-(Poly_Coef(1)*X.^3+Poly_Coef(2)*X.^2+Poly_Coef(3)*X+Poly_Coef(4)).';
                
                scint{iCh}.phi_filtered            = [scint{iCh}.phi_filtered(2:end);...
                    (varphiFilter.b_coef*Phi_res((end-length( varphiFilter.b_coef)+1):(end))-...
                    varphiFilter.a_coef(1:(end-1))*scint{iCh}.phi_filtered((end-length( varphiFilter.a_coef)+2):(end)))/...
                    varphiFilter.a_coef(end)];
                
            end
            % Calculate values of Signal has been locked