function [scint]=scintillation_postprocessing(path,refCh)
% Function performs scintillation processing of logged data
%


%--- User settings --------------------------------------------------------
if (~exist('path', 'var'))
    error('plot_logs_mat_file: file %s does not exist', filename);
end

if (~exist('refCh', 'var'))
    refCh         = 1;
end

fnMatLog    = 'logdata.mat';
fnSettings  = 'recsettings.mat';

%--- load rec eiver settings -----------------------------------------------
filename = fullfile(path, fnSettings);
if ( ~exist(filename, 'file') )
    error('plot_logs_mat_file: file %s does not exist', filename);
end
load(filename, 'recSettings');


%--- Data collected in tracking state -------------------------------------
filename = fullfile(path, fnMatLog);
if ( ~exist(filename, 'file') )
    error('plot_logs_mat_file: file %s does not exist', filename);
end
logInfo = who('-file', filename);
load(filename, 'tm1');
nCh = length(tm1);
clear('tm1');
% collect data in one big array
iDataSet    = 1;
iStateMsg   = 0;

stateMsg    = cell(100, 1);
ix          = 0;

T_average_S4                    = 1000*60;
T_average_varphi                = 1000*60;
ix                   = 0;
Phi_ref                      = 0;
for iCh=1:nCh
    scint{iCh}.ix_pause             = 0;
    scint{iCh}.I                    = [];
    scint{iCh}.phi                  = zeros(6,1);
    scint{iCh}.phi_filtered         = zeros(6,1);
    scint{iCh}.NBPminusWBP          = 10^13*ones(7+T_average_S4 /20,1);
    scint{iCh}.NBPminusWBP_detrend  = 10^13*ones(7+T_average_S4 /20,1);
    scint{iCh}.NBPminusWBP_detrend1 = 10^13*ones(7+T_average_S4 /20,1);
    scint{iCh}.NBPminusWBP_detrend2 = 10^13*ones(7+T_average_S4 /20,1);
    scint{iCh}.varphi               = 0;
    scint{iCh}.S4                   = 0;
    scint{iCh}.time                   = 59;
end
% Highpass Butterworth Filter for phi detrendeing
fCutOff                                                 = 0.2;
fSample                                                 = 50;
fNormalized                                             = 2*fCutOff/fSample;
[b,a]                                                   = butter(6,fNormalized,'high');
[gd,~]                                                  = grpdelay(b,a);
varphi_group_delay          = 3*round(max(gd));
varphi_b_coef               = fliplr(b);
varphi_a_coef               = fliplr(a);

% Lowpass Butterworth Filter for S4 detrending
fCutOff                                                 = 0.1;
fSample                                                 = 50;
fNormalized                                             = 2*fCutOff/fSample;
[b,a]                                                   = butter(2,fNormalized,'low');
[gd,~]                                                  = grpdelay(b,a);
s4_group_delay          = 3*round(max(gd));
s4_b_coef             = fliplr(b);
s4_a_coef             = fliplr(a);
while (true)
    sVarName = sprintf('tm%d', iDataSet);
    if (~any(strcmp(logInfo, sVarName)))
        break;
    end
    load(filename, sVarName);
    eval(['tmp = ', sVarName, ';']);
    for iCh = 1: nCh
        tm{iCh}.filePos_Ns       = tmp{iCh}.filePos_Ns;
        tm{iCh}.IQ         = tmp{iCh}.corrSign;
        iPrompt            = ceil(size(tm{iCh}.IQ,2)/2);
        tm{iCh}.CN0_PLL     = tmp{iCh}.lock.CN0_PLL;
        tm{iCh}.flPessimPLL     = tmp{iCh}.lock.flPessimPLL;
        tm{iCh}.flPessimDLL  = tmp{iCh}.lock.flPessimDLL;
        tm{iCh}.flPessimIQbal  = tmp{iCh}.lock.flPessimIQbal;
        if     sum(tmp{iCh}.trkTime_ms>0)
            tm{iCh}.phNCOScint    = tmp{iCh}.carr.phNCOScint;
        end
        tm{iCh}.flPessimPLL(isnan(tm{iCh}.flPessimPLL)) = 0;
        tm{iCh}.flPessimPLL(isnan(tm{iCh}.flPessimDLL)) = 0;
        tm{iCh}.flPessimPLL(isnan(tm{iCh}.flPessimIQbal)) = 0;
    end
    nSamples                 = length(tm{1}.filePos_Ns);
    for iS = 1:nSamples
        ix = ix+1;
        if tm{refCh}.flPessimPLL(iS)
            Phi_ref = tm{refCh}.phNCOScint(iS);
        else
            Phi_ref = 0;
        end
        
        for iCh = 1:nCh
            if     sum(tmp{iCh}.trkTime_ms>0)
                if iCh ~= refCh
                    %% Save detrended Intensity and Phase Measurements for Parameter Estimation
                    % Check for lock
                    if tm{iCh}.flPessimDLL(iS) ==1 && tm{iCh}.flPessimIQbal(iS)
                        scint{iCh}.I                           = [     scint{iCh}.I ,tm{iCh}.IQ(iS,iPrompt)];
                    else
                        scint{iCh}.I                           = [];
                        scint{iCh}.ix_pause                    = scint{iCh}.ix_pause +1 ;
                    end
                    % Check for full bit epoch
                    if length( scint{iCh}.I ) == 20
                        % Check for bit synchronization
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
                                    (s4_b_coef*    NBPminusWBPraw((end-i-length( s4_b_coef)+1):(end-i))-...
                                    s4_a_coef(1:(end-1))*  scint{iCh}.NBPminusWBP_detrend1((end-length( s4_a_coef)+2):(end)))/...
                                    s4_a_coef(end)];
                                scint{iCh}.NBPminusWBP_detrend2 = [  scint{iCh}.NBPminusWBP_detrend2((2):end) ;...
                                    (s4_b_coef*     scint{iCh}.NBPminusWBP_detrend1((end-length(s4_b_coef)+1):(end))-...
                                    s4_a_coef(1:(end-1))*  scint{iCh}.NBPminusWBP_detrend2((end-length( s4_a_coef)+2):(end)))/...
                                    s4_a_coef(end)];
                                scint{iCh}.NBPminusWBP_detrend = [  scint{iCh}.NBPminusWBP_detrend((2):end);...
                                    (s4_b_coef*    scint{iCh}.NBPminusWBP_detrend2((end-length( s4_b_coef)+1):(end))-...
                                    s4_a_coef(1:(end-1))*  scint{iCh}.NBPminusWBP_detrend((end-length( s4_a_coef)+2):(end)))/...
                                    s4_a_coef(end)];
                            end
                            
                            scint{iCh}.ix_pause                = 0;
                            scint{iCh}.I                       = [];
                        else
                            % Count pauses and erase first intensity measure
                            scint{iCh}.I                       =  scint{iCh}.I(2:end);
                            scint{iCh}.ix_pause                =  scint{iCh}.ix_pause +1 ;
                        end
                    end
                    
                    %% Store detrended Phase measurements
                    if mod(ix,20)==0
                        if ix <T_average_varphi
                            if tm{iCh}.flPessimPLL(iS)
                                scint{iCh}.phi                 = [scint{iCh}.phi; tm{iCh}.phNCOScint(iS)-Phi_ref];
                            else
                                scint{iCh}.phi                 = [scint{iCh}.phi; scint{iCh}.phi(end)];
                            end
                        elseif ix == T_average_varphi
                            if tm{iCh}.flPessimPLL(iS)
                                scint{iCh}.phi                 = [scint{iCh}.phi; tm{iCh}.phNCOScint(iS)-Phi_ref];
                            else
                                scint{iCh}.phi                 = [scint{iCh}.phi; scint{iCh}.phi(end)];
                            end
                            X                                           = linspace(ix - T_average_varphi ,ix,length(scint{iCh}.phi));
                            Poly_Coef                                   = polyfit(X(:),scint{iCh}.phi(:),3);
                            Phi_res                                     = scint{iCh}.phi(:)-(Poly_Coef(1)*X.^3+Poly_Coef(2)*X.^2+Poly_Coef(3)*X+Poly_Coef(4)).';
                            for i= 1:length(Phi_res)-6
                                scint{iCh}.phi_filtered        = [scint{iCh}.phi_filtered;...
                                    (varphi_b_coef*Phi_res(i:(i+6))-...
                                    varphi_a_coef(1:(end-1))*scint{iCh}.phi_filtered((end-length( varphi_a_coef)+2):(end)))/...
                                    varphi_a_coef(end)];
                            end
                            
                        elseif ix > T_average_varphi
                            if tm{iCh}.flPessimPLL(iS)
                                scint{iCh}.phi                 = [scint{iCh}.phi(2:end); tm{iCh}.phNCOScint(iS)-Phi_ref];
                            else
                                scint{iCh}.phi                 = [scint{iCh}.phi(2:end); scint{iCh}.phi(end)];
                            end
                            X                                           = linspace(ix - T_average_varphi ,ix,length(scint{iCh}.phi));
                            Poly_Coef                                   = polyfit(X(:),scint{iCh}.phi(:),3);
                            Phi_res                                     = scint{iCh}.phi(:)-(Poly_Coef(1)*X.^3+Poly_Coef(2)*X.^2+Poly_Coef(3)*X+Poly_Coef(4)).';
                            
                            scint{iCh}.phi_filtered            = [scint{iCh}.phi_filtered(2:end);...
                                (varphi_b_coef*Phi_res((end-length( varphi_b_coef)+1):(end))-...
                                varphi_a_coef(1:(end-1))*scint{iCh}.phi_filtered((end-length( varphi_a_coef)+2):(end)))/...
                                varphi_a_coef(end)];
                            
                        end
                        % Calculate values of Signal has been locked
                        
                    end
                    %% Scintillation Processor
                    if ix >= T_average_S4   && mod(ix,1000)==0
                        %% Calculation of S4
                        % Calculate actual Intensity with 50 Hz sampling rate
                        SI_50Hz                     =  scint{iCh}.NBPminusWBP(1:(end-s4_group_delay)) ./ scint{iCh}.NBPminusWBP_detrend((1+s4_group_delay):end);
                        SI_50Hz(isnan(SI_50Hz))     =  0;
                        CN0_linear                  = 10^(tm{iCh}.CN0_PLL(iS)/10);
                        S4_noise                    = (100/CN0_linear)*(1+(500/(19*CN0_linear)));
                        % S_4
                        scint{iCh}.S4      = [scint{iCh}.S4,sqrt(( mean(SI_50Hz.^2)- mean(SI_50Hz)^2)/mean(SI_50Hz)^2-S4_noise)];
                    end
                    if ix >= T_average_varphi   && mod(ix,1000)==0
                        %% Calculate varphi
                        % Only consider locked phi
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
                        
                    end
                    
                    
                    
                end
            end
        end
    end
    iDataSet = iDataSet + 1;
    clear(sVarName,'tm');
end
prn =[];
legendentries = [];
for iCh =1:length(tmp)
    if iCh ~= refCh && sum(tmp{iCh}.trkTime_ms>0)
        prn = [prn,tmp{iCh}.prn];
        legendentries{length(legendentries)+1} = ['PRN: ',num2str(tmp{iCh}.prn)];
    end
end
figure(1)
hold on
subplot(2,1,1)
title('S4')
hold on
data_s4 =[];
for iCh = 1:nCh
    if iCh ~= refCh && sum(tmp{iCh}.trkTime_ms>0)
        data_s4=[data_s4,scint{iCh}.S4(2:end)'];
    end
end
plot(scint{3}.time(2:end)',data_s4)
legend(legendentries)
subplot(2,1,2)
title('\sigma_\phi')
hold on
data_varphi=[];
for iCh = 1:nCh
    if iCh ~= refCh
        data_varphi=[data_s4,scint{iCh}.varphi(2:end)'];
    end
end
plot(scint{3}.time(2:end)',data_varphi)
end