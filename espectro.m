%% 10/12/2024 - Olympio\Douglas
% Realiza a medida de amplidude e fase dos canais 1 e 2 
% mostrando resultados no analisador de espectro e no osciloscopio para cada rodada de medida. 
% Salva resultados em uma tabela de resultados (matriz de structure)


%% Inicializaçao do radio
clear all; close all; clc

CFreq = 1575.42e6; % Frequencia central

Ganho = 30; % em dB
BW = 5e6; % Define a largura de banda
OverSampling = 1.0; % Define a taxa de subreamostragem (Nyquist*OverSampling)
SperFrame = 2048; % Define a quantidade de amostras por Frame de Aquisiçao

% Cria um objeto receptor: Duplo canal com sincronizaçao de fase entre eles
X310_RX = comm.SDRuReceiver(Platform="X310", IPAddress='192.168.10.2', CenterFrequency = CFreq, ...
    IsTwinRXDaughterboard = true, EnableTwinRXPhaseSynchronization = true, ...
    ChannelMapping=[1 2], ...
    OutputDataType="double"); 

%% Define diretrizes de sinal de aquisiçao 

% Calcula os parametros de configuraçao do radio
Srate = 1*BW*OverSampling; 
%GainCal = [1+Ganho 1+Ganho]; %[38.1+Ganho 37.8+Ganho]; % Insere calibraçao de ganho dos canais
GainCal = [38.1+Ganho 37.8+Ganho];
DecFactor = floor(X310_RX.MasterClockRate/Srate); % Calcrelease(RX310_ula o fator de decimaçao necessario para obter a sample rate
SrateEf = X310_RX.MasterClockRate/DecFactor; % Calcula a taxa de amostragem efetiva programada no radio
TSampEf = 1/SrateEf;
TperFrame = SperFrame*TSampEf;

% Envia parametros para o radio
release(X310_RX);
X310_RX.CenterFrequency = CFreq;
X310_RX.DecimationFactor = DecFactor;
X310_RX.Gain = GainCal;
X310_RX.SamplesPerFrame = SperFrame; 

% Verifica execuçao da inserçao de parametros 
Ck_X310_RX = info(X310_RX);% depois sera necessario criar um funçao conferir esses valores e emitir mensanes de erro

%% Cria objeto analisar do espectro e osciloscopio e apresenta janelas
DEA = spectrumAnalyzer(SampleRate=SrateEf,ReferenceLoad=50,CenterFrequency=CFreq); % Cria objeto analisador de espectro
DOSC = timescope(SampleRate=SrateEf); % Cria objeto osciloscopio
DOSC_Ap = timescope(SampleRate=SrateEf); % Cria objeto osciloscopio para plotar amplitude
DOSC_Ph = timescope(SampleRate=SrateEf); % Cria objeto osciloscopio para plotar fase
AOA_Fisico = 0;

NumMe = 1;

X310_RX.CenterFrequency = CFreq; % Programa a frequencia central no RDS
Ck_X310_RX = info(X310_RX);% depois sera necessario criar um funçao conferir esses valores e emitir mensanes de erro
Ck_X310_RX.CenterFrequency % Verifica se o comando de programaçao foi executado 
while CFreq ~= 0
    
    for ii = 1 : 10 %Loop necessario por causa da 'm' do analisador de espectro que tem uma "p" de uma media que nao consigo tirar
        AcqData =  X310_RX(); % Faz aquisiçao do frame de sinal
        DEA(AcqData); % Envia vetor de aquisiçao para analisador de espectro
        DOSC(real(AcqData));% Envia vetor de aquisiçao para osciloscopio com o sinal real dos canais 1 e 2
    end
    %AcqData =  X310_RX(); % Faz aquisiçao do frame de sinal
end



release(X310_RX)
% findsdru()
% nomeDoArquiovo = 'MedAmpFaseArPlano(4G_30Deg_22-10-2024)';
% save(nomeDoArquiovo,"TabMed")
% TabMedRec = load(nomeDoArquiovo)


save AcqData