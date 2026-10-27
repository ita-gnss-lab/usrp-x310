clear
close
clc

%% File Specification
path = 'H:\Fortaleza2016_06_27_-_16.54.56'; %Path with data to parse
gnssTypes = {'GPS'}; % Tracked GNSS Types
gnssBands = {'L1','L5'}; %Tracked GNSS Bands

%% Processing Specifications
% Averaging Time
param.T_average_S4                    = 60000;
param.T_average_varphi                = 60000;
param.iIntPeriod                      = 0;
param.Phi_ref                         = 0;
iDataSet                        = 1;
stateMsg                        = cell(100, 1);

% Detrending Filters
% Highpass Butterworth Filter for phi detrendeing
fCutOff                                                 = 0.2;
fSample                                                 = 50;
fNormalized                                             = 2*fCutOff/fSample;
[b,a]                                                   = butter(6,fNormalized,'high');
[gd,~]                                                  = grpdelay(b,a);
varphiFilter.group_delay          = 3*round(max(gd));
varphiFilter.b_coef               = fliplr(b);
varphiFilter.a_coef               = fliplr(a);

% Lowpass Butterworth Filter for S4 detrending
fCutOff                                                 = 0.1;
fSample                                                 = 50;
fNormalized                                             = 2*fCutOff/fSample;
[b,a]                                                   = butter(2,fNormalized,'low');
[gd,~]                                                  = grpdelay(b,a);
s4Filter.group_delay          = 3*round(max(gd));
s4Filter.b_coef             = fliplr(b);
s4Filter.a_coef             = fliplr(a);
%% Opening File and Check for Files
if (~exist('path', 'var'))
    error('plot_logs_mat_file: file %s does not exist', filename);
end

if (~exist('refCh', 'var'))
    refCh         = 1;
end

fnMatLog    = 'logdata.mat';
fnSettings  = 'recsettings.mat';
fnScintProc = 'ScintProc.mat';
% Check wether Scintillation Processor Data is saved
filename = fullfile(path, fnScintProc);
if ( ~exist(filename, 'file') )
    % If not run Scintillation Processor
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
    
    %% Initialize Scintillation Processor
    for iCh=1:nCh
        scint{iCh}.ix_pause             = 0;
        scint{iCh}.I                    = [];
        scint{iCh}.phi                  = zeros(6,1);
        scint{iCh}.phi_filtered         = zeros(6,1);
        scint{iCh}.NBPminusWBP          = 10^13*ones(7+param.T_average_S4 /20,1);
        scint{iCh}.NBPminusWBP_detrend  = 10^13*ones(7+param.T_average_S4 /20,1);
        scint{iCh}.NBPminusWBP_detrend1 = 10^13*ones(7+param.T_average_S4 /20,1);
        scint{iCh}.NBPminusWBP_detrend2 = 10^13*ones(7+param.T_average_S4 /20,1);
        scint{iCh}.varphi               = 0;
        scint{iCh}.S4                   = 0;
        scint{iCh}.time                 = 59;
    end
    plotInfos.isTracked = zeros(nCh,1);
    plotInfos.gnssType =cell(nCh,1);
    plotInfos.gnssBand = cell(nCh,1);
    plotInfos.PRN = zeros(nCh,1);
    while (true)
        % Allociere naechsten Variablen Namen
        sVarName = sprintf('tm%d', iDataSet);
        
        % Check wether variable is contained in logdate
        if (~any(strcmp(logInfo, sVarName)))
            break; %if not break and start plotting
        end
        %load Variable
        load(filename, sVarName);
        eval(['tmp = ', sVarName, ';']);
        
        % Read Parameters from Variable
        for iCh = 1:nCh
            if ~isempty(tmp{iCh}.alloc)
                plotInfos.isTracked(iCh) =1;
                plotInfos.gnssType{iCh} = tmp{iCh}.alloc{1}.gnssType;
                plotInfos.gnssBand{iCh} = tmp{iCh}.alloc{1}.gnssBand;
                plotInfos.PRN(iCh) =tmp{iCh}.alloc{1}.prn;
            end
            tm{iCh}          = scintProc_evalTM(tmp{iCh},plotInfos.isTracked(iCh));
        end
        nSamples           = length(tm{iCh}.filePos_Ns);
        iPrompt            = ceil(size(tm{iCh}.IQ,2)/2);
        %% Iterate through all Samples and Channels
        for iS = 1:nSamples
            param.iIntPeriod = param.iIntPeriod+1;
            % Get reference Phi from Channel refCh without Scintillation
            if tm{refCh}.flPessimPLL(iS)
                param.Phi_ref = tm{refCh}.phNCOScint(iS);
            else
                param.Phi_ref = 0;
            end
            
            for iCh = 1 :nCh
                if iCh ~= refCh
                    %% Save detrended Intensity and Phase Measurements for Parameter Estimation
                    % Check for lock
                    if tm{iCh}.flPessimDLL(iS) && tm{iCh}.flPessimIQbal(iS) %% We want that ?
                        scint{iCh}.I                           = [     scint{iCh}.I ,tm{iCh}.IQ(iS,iPrompt)];
                    else
                        scint{iCh}.I                           = [];
                        scint{iCh}.ix_pause                    = scint{iCh}.ix_pause +1 ;
                    end
                    
                    if length( scint{iCh}.I ) == 20
                        % Perform Wide and Narrowband filtering if full bit is
                        % reached
                        scint = scintProc_S4detrend(iCh,scint,s4Filter);
                    end
                    if mod(param.iIntPeriod,20)==0
                        %Detrend Phase Estimate
                        scint = scintProc_varphidetrend(iCh,iS,scint,varphiFilter,param,tm);
                    end
                    if param.iIntPeriod >= param.T_average_S4   && mod(param.iIntPeriod,1000)==0
                        %Calculate S4
                        scint = scintProc_calcS4(scint,tm,s4Filter,iCh,iS);
                    end
                    if param.iIntPeriod >= param.T_average_varphi   && mod(param.iIntPeriod,1000)==0
                        % Calculate varphi
                        scint = scintProc_calcvarphi(scint,iCh);
                        
                    end
                    
                end
            end
            
        end
        
        iDataSet = iDataSet + 1;
        clear(sVarName,'tm');
    end
    %save Data to save Time for next time
    filename = fullfile(path, fnScintProc);
    save(filename,'plotInfos','scint');
else
    %load saved Scintillation Processor data
    load(filename);
end
%% Plotting
% Get GNSS Type and Band in Data
plotInfos.GenPlot = cell(length(gnssTypes)*length(gnssBands),1);
nPlot = length(gnssTypes)*length(gnssBands);
for iPlot = 1:nPlot
    plotInfos.GenPlot{iPlot}.legendentries={};
    plotInfos.GenPlot{iPlot}.dataS4=[];
    plotInfos.GenPlot{iPlot}.dataPhi =[];
    plotInfos.GenPlot{iPlot}.time =[];
end
iPlot = 1;
ixCh = [];
% Generate Legendentries and Titles
for iType = 1:length(gnssTypes)
    for iBand = 1:length(gnssBands)
        % Read Channels with respective Type and Band from plot Infos
        for iCh = 1:length(plotInfos.isTracked)
            tmp_gnssType=plotInfos.gnssType{iCh};
            tmp_gnssBand=plotInfos.gnssBand{iCh};
            if strcmp(plotInfos.gnssType{iCh},gnssTypes{iType}) && strcmp(plotInfos.gnssBand{iCh},gnssBands{iBand}) && iCh ~=refCh
                ixCh = [ixCh,iCh];
            end
        end
        
        for iCh= ixCh
            if plotInfos.isTracked(iCh) && iCh ~= refCh
                plotInfos.GenPlot{iPlot}.legendentries{length( plotInfos.GenPlot{iPlot}.legendentries)+1} = ['PRN: ',num2str(plotInfos.PRN(iCh))];
                plotInfos.GenPlot{iPlot}.dataS4 =[ plotInfos.GenPlot{iPlot}.dataS4,scint{iCh}.S4(2:end)'];
                plotInfos.GenPlot{iPlot}.dataPhi =[ plotInfos.GenPlot{iPlot}.dataPhi,scint{iCh}.varphi(2:end)'];
                if isempty( plotInfos.GenPlot{iPlot}.time)
                 plotInfos.GenPlot{iPlot}.time = scint{iCh}.time(2:end)';
                end
            end
        end
        plotInfos.GenPlot{iPlot}.titleS4 =['S_4 for ',gnssTypes{iType},' ',gnssBands{iBand}];
        plotInfos.GenPlot{iPlot}.titlePhi =['\sigma_\phi for ',gnssTypes{iType},' ',gnssBands{iBand}];
        iPlot =iPlot+1;
        ixCh = [];
    end
end

for iPlot = 1:nPlot
    if ~isempty(plotInfos.GenPlot{iPlot}.time)
        figure(iPlot)
        hold on
        subplot(2,1,1)
        ylim([0,1]);
        title(plotInfos.GenPlot{iPlot}.titleS4)
        hold on
        set(gca,'LineStyleOrder', '-o|-s|-+')
        plot( plotInfos.GenPlot{iPlot}.time,plotInfos.GenPlot{iPlot}.dataS4)
        legend(plotInfos.GenPlot{iPlot}.legendentries)
        xlabel('Processing Time in s')
        ylabel('S_4')
        subplot(2,1,2)
        title(plotInfos.GenPlot{iPlot}.titlePhi)
        hold on
        plot( plotInfos.GenPlot{iPlot}.time,plotInfos.GenPlot{iPlot}.dataPhi*180/pi)
        xlabel('Processing Time in s')
        ylabel('\sigma_\phi')
    end
end
