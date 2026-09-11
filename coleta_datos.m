%% 16/04/2024 - Olympio
% Realiza a medida de amplidude e fase dos canais 1 e 2 
% mostrando resultados no analisador de espectro e no osciloscopio para cada rodada de medida. 
% Salva resultados em uma tabela de resultados (matriz de structure)


%% Inicializaçao do radio
clear all; close all; clc

CFreq = 1575.42e6; % Frequencia central

Ganho = 1; % em dB
BW = 2e6; % Define a largura de banda
OverSampling = 1.0; % Define a taxa de subreamostragem (Nyquist*OverSampling)
SperFrame = 2048; % Define a quantidade de amostras por Frame de Aquisiçao
AcqTime = 5 ;% Tempo de aquisicao
output_filename="experimento.mat";


prompt = {'Enter the center frequency [Hz]:','Enter the Gain [dB]:',...
    "Enter the Bandwidth: [Hz]", "Enter the oversampling factor: "...
    "Enter the number of samples per acquisition: ","Total acquisition time: ","Filename to save: ",};
dlgtitle = 'Coleta de Dados X310';
fieldsize = [1 45; 1 45; 1 45; 1 45; 1 45; 1 45; 1 45];
definput = {'1575.42e6','1', '2e6', '1.0','2048','2','experimento.mat'};
answer = inputdlg(prompt,dlgtitle,fieldsize,definput);

CFreq = str2num(answer{1}); % Frequencia central

Ganho = str2num(answer{2}); % em dB
BW = str2num(answer{3}); % Define a largura de banda
OverSampling = str2num(answer{4}); % Define a taxa de subreamostragem (Nyquist*OverSampling)
SperFrame = str2num(answer{5}); % Define a quantidade de amostras por Frame de Aquisiçao
AcqTime = str2num(answer{6}) ;% Tempo de aquisicao
output_filename=answer{7};


% Cria um objeto receptor: Duplo canal com sincronizaçao de fase entre eles
X310_RX = comm.SDRuReceiver(Platform="X310", IPAddress='192.168.10.2', CenterFrequency = CFreq, ...
    IsTwinRXDaughterboard = true, EnableTwinRXPhaseSynchronization = true, EnableTimeTrigger = true, ...
    ChannelMapping=[1 2], ...
    OutputDataType="double"); 

%% Define diretrizes de sinal de aquisiçao 

% Calcula os parametros de configuraçao do radio
Srate = 1*BW*OverSampling; % Amostras/Seg
GainCal = [Ganho Ganho]; %[38.1+Ganho 37.8+Ganho]; % Insere calibraçao de ganho dos canais

%GainCal = [38.1+Ganho 37.8+Ganho];
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

%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% alteracao
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% salva


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
total_acq = [];
i = 0;
while size(total_acq,1)<Srate*AcqTime
        
    AcqData =  X310_RX(); % Faz aquisiçao do frame de sinal
    total_acq = [total_acq; AcqData];
    disp(size(total_acq,1)/Srate)

end

release(X310_RX)
% findsdru()
% nomeDoArquiovo = 'MedAmpFaseArPlano(4G_30Deg_22-10-2024)';
% save(nomeDoArquiovo,"TabMed")
% TabMedRec = load(nomeDoArquiovo)

save(output_filename,'total_acq')

figure;
plot(real(total_acq(1:1000,1)))
hold on
plot(real(total_acq(1:1000,2)))



N = 10000;
fs = 10e6; % ajuste se necessário

X1 = fftshift(fft(total_acq(1:N,1), N));
X2 = fftshift(fft(total_acq(1:N,2), N));
f = linspace(-fs/2, fs/2, N);

figure
plot(f/1e6, 20*log10(abs(X1)/max(abs(X1))));
hold on;
plot(f/1e6, 20*log10(abs(X2)/max(abs(X2))));
legend('Canal 1','Canal 2');
xlabel('Frequência (MHz)'); ylabel('Magnitude (dB)');
title('Espectro dos sinais recebidos');
grid on;



% Seleciona um trecho representativo (descarta os primeiros samples nulos)
idx_ini = 1;
idx_fim = idx_ini + 100000;

idx_fim = size(total_acq,1);

sig1 = total_acq(idx_ini:idx_fim, 1);
sig2 = total_acq(idx_ini:idx_fim, 2);

% Diferença de fase
phase_diff = angle(sig1) - angle(sig2);
phase_diff = wrapToPi(phase_diff); % mantém no intervalo [-pi, pi]

% Plot
figure;
plot(phase_diff);
title('Diferença de fase entre canais 1 e 2');
xlabel('Amostra');
ylabel('Fase (rad)');
grid on;

[xcorr_val, lag] = xcorr(sig1, sig2);
[~, max_idx] = max(abs(xcorr_val));
delay_in_samples = lag(max_idx);

delay_in_samples/fs

disp(['Atraso em amostras entre os canais: ', num2str(delay_in_samples)]);

N = 1000000;
figure;
plot(abs(total_acq(1+delay_in_samples:N+delay_in_samples,1)))
hold on
plot(abs(total_acq(1:N,2)))


figure;
plot(angle(total_acq(1+delay_in_samples:N+delay_in_samples,1)))
hold on
plot(angle(total_acq(1:N,2)))


