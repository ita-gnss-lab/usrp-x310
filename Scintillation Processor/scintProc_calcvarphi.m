function scint = scintProc_calcvarphi(scint,iCh)
            Phi                         = scint{iCh}.phi_filtered;
            
            DIF                         = Phi(1:end-1)-Phi(2:end);
            JUMP                        = abs(DIF)>0.9;
            
            % Get indices of jumps
            LOOKUP                      = 1:length(DIF);
            jump_idx                    = LOOKUP(JUMP);
            jump_idx                    = [0,jump_idx,length(Phi)];
            Interv_length               = jump_idx(2:end)-jump_idx(1:end-1);
            varphi                      = zeros(1,length(jump_idx)-1);
            
            % Calculate varphi for each intervall without jump
            for i= 1:length(jump_idx)-1
                Phi_work                = Phi((jump_idx(i)+1):jump_idx(i+1));
                varphi(i)               = sqrt( mean(Phi_work.^2)- mean(Phi_work)^2);
            end
            
            % Calculate weighted sum
            scint{iCh}.varphi  = [scint{iCh}.varphi,sum(Interv_length.*varphi)/sum(length(Phi))];
            scint{iCh}.time = [  scint{iCh}.time ,  scint{iCh}.time(end)+1];