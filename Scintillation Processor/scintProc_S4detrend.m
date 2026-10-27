function     scint = scintProc_S4detrend(iCh,scint,s4filter)
            I_val                                       = real(scint{iCh}.I);
            Q_val                                       = imag(scint{iCh}.I);
            if abs(sum(sign( I_val))) == 20
                
                jumped_epochs                               = ceil(scint{iCh}.ix_pause/20);
                
                % calulate and store NBP-WBP for noise cancellation
                scint{iCh}.NBPminusWBP             = [scint{iCh}.NBPminusWBP((2+jumped_epochs):end); scint{iCh}.NBPminusWBP(end)*ones(jumped_epochs,1);sum( I_val)^2+sum( Q_val)^2-sum( I_val.^2+Q_val.^2)];
                NBPminusWBPraw                              = scint{iCh}.NBPminusWBP(:);
                % Detrending with Butterworth filter
                for i =jumped_epochs:-1:0
                    scint{iCh}.NBPminusWBP_detrend1 = [ scint{iCh}.NBPminusWBP_detrend1((2):end) ;...
                        (s4filter.b_coef*    NBPminusWBPraw((end-i-length( s4filter.b_coef)+1):(end-i))-...
                        s4filter.a_coef(1:(end-1))*  scint{iCh}.NBPminusWBP_detrend1((end-length( s4filter.a_coef)+2):(end)))/...
                        s4filter.a_coef(end)];
                    scint{iCh}.NBPminusWBP_detrend2 = [  scint{iCh}.NBPminusWBP_detrend2((2):end) ;...
                        (s4filter.b_coef*     scint{iCh}.NBPminusWBP_detrend1((end-length(s4filter.b_coef)+1):(end))-...
                        s4filter.a_coef(1:(end-1))*  scint{iCh}.NBPminusWBP_detrend2((end-length( s4filter.a_coef)+2):(end)))/...
                        s4filter.a_coef(end)];
                    scint{iCh}.NBPminusWBP_detrend = [  scint{iCh}.NBPminusWBP_detrend((2):end);...
                        (s4filter.b_coef*    scint{iCh}.NBPminusWBP_detrend2((end-length( s4filter.b_coef)+1):(end))-...
                        s4filter.a_coef(1:(end-1))*  scint{iCh}.NBPminusWBP_detrend((end-length( s4filter.a_coef)+2):(end)))/...
                        s4filter.a_coef(end)];
                end
                
                scint{iCh}.ix_pause                = 0;
                scint{iCh}.I                       = [];
            else
                % Count pauses and erase first intensity measure
                scint{iCh}.I                       =  scint{iCh}.I(2:end);
                scint{iCh}.ix_pause                =  scint{iCh}.ix_pause +1 ;
            end