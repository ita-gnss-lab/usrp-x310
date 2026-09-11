clear
close all
clc

load('experimento_out_85.mat')
rxwaveform = total_acq(:, 2);  % sinal real de 1 canal
fs = 2e6;   

acq = gnssSignalAcquirer;
acq.SampleRate = fs;
acq.IntermediateFrequency = 0;  % se você gravou em baseband
acq.FrequencyRange = [-10000 10000];
acq.FrequencyResolution = 250;
acq.DetectionThresholdFactor = 1.2;

N = fs * 1e-3;  % 2 ms
sig = rxwaveform(10000 : 10000 + N - 1);  % evitar transientes iniciais

[acq_table, corr_matrix] = acq(sig, 1:32); % corr_matrix P X Q X R -> P: number of code-phase offsets in the search, and is equal to the length of signal -> Q: the number of frequency offset bins in the search. -> R: the number of searched satellites.


detected_idx = find(acq_table.IsDetected);
prn_detected = acq_table.PRNID(detected_idx);
doppler = acq_table.FrequencyOffset(detected_idx);
codephase = acq_table.CodePhaseOffset(detected_idx);

idx = 1;

% Escolha o PRN que você quer visualizar (exemplo: PRN 15)
prn_id = acq_table.PRNID(idx);

% Obter índices de Doppler e code phase
doppler_axis = acq.FrequencyRange(1) : acq.FrequencyResolution : acq.FrequencyRange(2);
code_phase_axis = 0:size(corr_matrix,1)-1;

% Encontrar o índice correspondente ao PRN desejado
prn_idx = find(acq_table.PRNID == prn_id);

% Plotar o resultado 3D da matriz de correlação
figure;
mesh(doppler_axis, code_phase_axis, corr_matrix(:,:,prn_idx));
xlabel('Doppler Offset (Hz)');
ylabel('Code Phase Offset (samples)');
zlabel('Correlation');
title(['Correlation Plot for PRN ID: ', num2str(prn_id)]);

%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%% 

tracker = gnssSignalTracker;
tracker.SampleRate = fs;
tracker.IntermediateFrequency = 0;
tracker.IntegrationTime = 1e-3;
tracker.PRNID = prn_detected;
tracker.InitialFrequencyOffset = doppler;
tracker.InitialCodePhaseOffset = codephase;
tracker.FLLNoiseBandwidth = 1;  % Experimente reduzir para 1 ou 2 Hz
tracker.PLLNoiseBandwidth = 15; % ou menos se necessário
tracker.DLLNoiseBandwidth = 1;

% Segmentar o sinal em janelas de 1 ms e rastrear
block_size = fs * tracker.IntegrationTime;
[rx_blocks, ~] = buffer(rxwaveform, block_size);
n_frames = size(rx_blocks, 2);

n_prn = numel(prn_detected);
freq_est = zeros(n_frames, n_prn);
phase_est = zeros(n_frames, n_prn);
delay_est = zeros(n_frames, n_prn);
integ_IQ = zeros(n_frames, 1);



for k = 1:n_frames
    [integwave, trackinfo] = tracker(rx_blocks(:,k));
    freq_est(k,:)  = trackinfo.FrequencyEstimate;
    phase_est(k,:) = trackinfo.PhaseEstimate;
    delay_est(k,:) = trackinfo.DelayEstimate;
    integ_IQ(k) = integwave(idx);  % ou `integwave(idx)` para um PRN específico
end

% Normalizar amplitude
integ_IQ = integ_IQ / rms(integ_IQ);

figure;
scatterplot(integ_IQ);
title(['Scatterplot I/Q - PRN ', num2str(prn_detected(1))]);
xlabel('In-phase (I)');
ylabel('Quadrature (Q)');
grid on;



figure;
subplot(3,1,1); plot(freq_est(:,idx)); title(['Doppler Estimado - PRN ', num2str(prn_detected(idx))]);
subplot(3,1,2); plot(phase_est(:,idx)); title('Fase Estimada');
subplot(3,1,3); plot(delay_est(:,idx)); title('Atraso Estimado');



ShowVisualizations = true; % se quiser controlar via flag

if ShowVisualizations
    tempo_ms = (0:n_frames-1) * tracker.IntegrationTime * 1000; % tempo em ms
    
    for isat = 1:1
        prn_id = prn_detected(isat);
        groupTitle = "Tracking Loop Results for Satellite PRN ID: " + prn_id;

        % FLL
        figure;
        subplot(2,1,1);
        plot(tempo_ms, [NaN; diff(freq_est(:,isat))/tracker.IntegrationTime]); % freq. error ~ derivada
        xlabel("Milliseconds");
        ylabel("Frequency Error");
        title("Frequency Discriminator Output");

        subplot(2,1,2);
        plot(tempo_ms, freq_est(:,isat));
        xlabel("Milliseconds");
        ylabel("Estimated Frequency Offset");
        title("FLL Output");

        sgtitle("FLL " + groupTitle);

        % PLL
        figure;
        subplot(2,1,1);
        plot(tempo_ms, [NaN; diff(phase_est(:,isat))/tracker.IntegrationTime]); % fase error ~ derivada
        xlabel("Milliseconds");
        ylabel("Phase Error");
        title("Phase Discriminator Output");

        subplot(2,1,2);
        plot(tempo_ms, phase_est(:,isat));
        xlabel("Milliseconds");
        ylabel("Estimated Phase");
        title("PLL Output");

        sgtitle("PLL " + groupTitle);

        % DLL
        figure;
        subplot(2,1,1);
        plot(tempo_ms, [NaN; diff(delay_est(:,isat))/tracker.IntegrationTime]); % delay error ~ derivada
        xlabel("Milliseconds");
        ylabel("Delay Error");
        title("Delay Discriminator Output");

        subplot(2,1,2);
        plot(tempo_ms, delay_est(:,isat));
        xlabel("Milliseconds");
        ylabel("Estimated Delay (samples)");
        title("DLL Output");

        sgtitle("DLL " + groupTitle);
    end
end


