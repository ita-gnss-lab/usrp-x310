clear
clc
load experimento.mat

% Aquisição inicial
gsa = gnssSignalAcquirer;
gsa.SampleRate = 2e6;
acqWindow = 2000;  % tamanho da janela de aquisição

% Faixa de amostras para rastrear
k = 1;
K = acqWindow;

% Aquisição do primeiro bloco para CH1
[Information_ch1, ~] = gsa(total_acq(k:K,1), 1:32);

% Seleciona os PRNs detectados
idx_detected = find(Information_ch1.IsDetected == 1);
idx_detected = idx_detected(1:min(8,end));  % limita a 8

detectedPRNs = Information_ch1.PRNID(idx_detected);
freqOffset   = Information_ch1.FrequencyOffset(idx_detected);
codeOffset   = Information_ch1.CodePhaseOffset(idx_detected);


% Inicializa rastreador com todos os sinais detectados
gst = gnssSignalTracker( ...
    SampleRate = 2e6, ...
    PRNID = detectedPRNs, ...
    InitialFrequencyOffset = freqOffset, ...
    InitialCodePhaseOffset = codeOffset);

% Inicializa variáveis de saída
nBlocks = floor(size(total_acq,1) / acqWindow);
freqHistory = zeros(nBlocks, length(detectedPRNs));

% Loop de rastreamento
for i = 1:nBlocks
    idx = (i-1)*acqWindow + 1 : i*acqWindow;
    signalBlock = total_acq(idx,1);  % canal 1

    [~, trackInfo] = gst(signalBlock);

    % Salva frequência estimada para cada PRN
    freqHistory(i, :) = trackInfo.FrequencyEstimate;
end

% Plot Doppler dos PRNs rastreados
figure;
plot(freqHistory);
xlabel('Bloco de tempo');
ylabel('Frequência Doppler estimada (Hz)');
legend("PRN " + string(detectedPRNs));
title('Evolução da frequência Doppler no rastreamento');
