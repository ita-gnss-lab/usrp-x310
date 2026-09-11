%% 16/04/2024 - Olympio
% Realiza a medida de amplidude e fase dos canais 1 e 2 
% mostrando resultados no analisador de espectro e no osciloscopio para cada rodada de medida. 
% Salva resultados em uma tabela de resultados (matriz de structure)
%% Inicializaçao do radio
clear all; close all; clc

% Cria um objeto receptor: Duplo canal com sincronizaçao de fase entre eles
X310_RX = comm.SDRuReceiver(Platform="X310", IPAddress='192.168.10.2', CenterFrequency = 2000e6, ...
    IsTwinRXDaughterboard = true, EnableTwinRXPhaseSynchronization = true, ...
    ChannelMapping=[1 2], ...
    OutputDataType="double"); 

%% Define diretrizes de sinal de aquisiçao 
CFreq = 1000e6; % Frequencia central

Ganho = 0; % em dB
BW = 2e6; % Define a largura de banda
OverSampling = 1.0; % Define a taxa de subreamostragem (Nyquist*OverSampling)
SperFrame = 4096; % Define a quantidade de amostras por Frame de Aquisiçao

% Calcula os parametros de configuraçao do radio
Srate = 1*BW*OverSampling; 
GainCal = [38.1+Ganho 37.8+Ganho]; % Insere calibraçao de ganho dos canais
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
DEA = spectrumAnalyzer(SampleRate=SrateEf,ReferenceLoad=50); % Cria objeto analisador de espectro
DOSC = timescope(SampleRate=SrateEf); % Cria objeto osciloscopio
DOSC_Ap = timescope(SampleRate=SrateEf); % Cria objeto osciloscopio para plotar amplitude
DOSC_Ph = timescope(SampleRate=SrateEf); % Cria objeto osciloscopio para plotar fase


NumMed = 1
%% Entrada de dados da primeira medida
    CFreq = 1e6*input("Entre frequencia central (MHz): ");
    AOA_Fisico = input("Entre angulo de chegada ajustado (Graus): ");
%% Roda o loop de medidas
while CFreq ~= 0
    X310_RX.CenterFrequency = CFreq; % Programa a frequencia central no RDS
    Ck_X310_RX = info(X310_RX);% depois sera necessario criar um funçao conferir esses valores e emitir mensanes de erro
    Ck_X310_RX.CenterFrequency % Verifica se o comando de programaçao foi executado 
    for ii = 1 : 1000 %Loop necessario por causa da 'm' do analisador de espectro que tem uma "p" de uma media que nao consigo tirar
        AcqData =  X310_RX(); % Faz aquisiçao do frame de sinal
        DEA(AcqData); % Envia vetor de aquisiçao para analisador de espectro
        DOSC(real(AcqData));% Envia vetor de aquisiçao para osciloscopio com o sinal real dos canais 1 e 2
        disp(ii)
    end
    AcqData =  X310_RX(); % Faz aquisiçao do frame de sinal
    %% Cria a tabela de resultados (Uma matriz de structure)
    TabMed(NumMed).CFreq = Ck_X310_RX.CenterFrequency;% Registra frequencia central do RDS
    TabMed(NumMed).AOA_Fisico = AOA_Fisico;% Registra o AOA fisico ajustado
    TabMed(NumMed).CHGain = Ck_X310_RX.Gain; % Registra os ganhos de cada canal do RDS
    TabMed(NumMed).SRate = Ck_X310_RX.BasebandSampleRate; % Registra a taxa de amostragem do RDS
    TabMed(NumMed).IQ = AcqData; % Registra os dados brutos de aquisiçao IQ medidos em cada canal
    TabMed(NumMed).AbsCH = abs(AcqData); % Calcula a amplitude instantanea de cada amostra
    TabMed(NumMed).PhaCH = angle(AcqData); % Calcula a fase instantanea de cada amostra
    TabMed(NumMed).AbsCHDif = abs(AcqData(:,1))-abs(AcqData(:,2)); % Calcula o desbalanceamento de amplitude entre os canais
    TabMed(NumMed).PhaCHDif = angle(AcqData(:,1))-angle(AcqData(:,2)); % Calcula o desbalanceamento de fase entre os canais
    %% Plota os resultado de cada medida
    close all;
    figure(); 
    plot(TabMed(NumMed).AbsCH);
    title('Amplitude');
    figure();
    plot(TabMed(NumMed).AbsCHDif);
    title('Desbalanceamento de Amplitude');
    figure();
    plot((TabMed(NumMed).PhaCHDif)*180/pi);
    title('Desbalanceamento de fase');
    figure();
    plot(real(TabMed(NumMed).IQ));
    title('Sinal Ch1 e Ch2');
    %% Entrada de dados de uma nova medida
    NumMed = NumMed + 1 
    % CFreq = 1e6*input("Entre frequencia central (MHz): ");
    % AOA_Fisico = input("Entre angulo de chegada ajustado (Graus): ");
    if NumMed == 11
        CFreq = 0;
    end
    pause(5)
end
release(X310_RX)
findsdru()
nomeDoArquiovo = 'MedAmpFaseArPlano(4G_30Deg_22-10-2024)';
save(nomeDoArquiovo,"TabMed")
TabMedRec = load(nomeDoArquiovo)


