clear
close all
clc
load experimento_dani_50.mat

%% === Parâmetros gerais ===
fs = 2e6;
acqWindow = 2000;
integrationSpan = 20;
nBlocks = floor(size(total_acq,1) / acqWindow) - integrationSpan + 1;
Ti = (acqWindow * integrationSpan) / fs;

%% === Aquisição inicial no primeiro bloco ===
gsa = gnssSignalAcquirer(SampleRate = fs);
[Information_ch1, ~] = gsa(total_acq(1:acqWindow, 1), 1:32);

% PRNs detectados (máx. 8)
idx_detected = find(Information_ch1.IsDetected == 1);
idx_detected = idx_detected(1:min(8, end));

detectedPRNs = Information_ch1.PRNID(idx_detected);
freqOffset   = Information_ch1.FrequencyOffset(idx_detected);
codeOffset   = mod(Information_ch1.CodePhaseOffset(idx_detected), 1023);
nPRNs = length(detectedPRNs);

%% === Inicializa rastreadores ===
gst = cell(1, nPRNs);
for j = 1:nPRNs
    gst{j} = gnssSignalTracker( ...
        SampleRate = fs, ...
        PRNID = detectedPRNs(j), ...
        InitialFrequencyOffset = freqOffset(j), ...
        InitialCodePhaseOffset = codeOffset(j));
end

%% === Inicializa históricos ===
freqHistory   = zeros(nBlocks, nPRNs);
delayHistory  = zeros(nBlocks, nPRNs);
phaseHistory  = zeros(nBlocks, nPRNs);
cn0History    = zeros(nBlocks, nPRNs);

%% === Loop principal ===
for i = 1:nBlocks
    idx = (i-1)*acqWindow + 1 : (i-1+integrationSpan)*acqWindow;
    sig_blk = total_acq(idx, 1);

    if i == 1
        figure;
        subplot(2,1,1); plot(real(sig_blk)); title('Sinal integrado - parte real');
        subplot(2,1,2); plot(abs(sig_blk));  title('Sinal integrado - módulo');
    end

    for j = 1:nPRNs
        tracker = gst{j};
        [y, info] = tracker(sig_blk);

        if i == 1
            figure;
            subplot(2,1,1); plot(real(y)); title("Parte real - PRN " + detectedPRNs(j));
            subplot(2,1,2); plot(abs(y));  title("Módulo - PRN " + detectedPRNs(j));
        end

        % Remove offset DC
        y = y - mean(y);

        % Potência do sinal
        Ps = mean(abs(y).^2);

        % Estimativa do ruído por amostras iniciais e finais
        tail_len = round(length(y)/10);
        noise_y = [y(1:tail_len); y(end-tail_len+1:end)];
        sigma2 = var(real(noise_y)) + var(imag(noise_y));

        % C/N₀ indireto
        if sigma2 > 0
            cn0_linear = Ps / (sigma2 * Ti);
            if cn0_linear > 1.1
                cn0History(i,j) = 10 * log10(cn0_linear);
            else
                cn0History(i,j) = NaN;
            end
        else
            cn0History(i,j) = NaN;
        end

        % Outras estimativas
        freqHistory(i,j)  = mean(info.FrequencyEstimate);
        delayHistory(i,j) = mean(info.DelayEstimate);
        phaseHistory(i,j) = mean(info.PhaseEstimate);

        % Diagnóstico textual
        fprintf("Bloco %d | PRN %2d | Ps = %.3e | σ² = %.3e | C/N₀ = %.2f dB-Hz\n", ...
            i, detectedPRNs(j), Ps, sigma2, cn0History(i,j));
    end
end

%% === Plots ===
figure;
subplot(2,1,1);
plot(freqHistory); ylabel('Doppler (Hz)');
title('Frequência Doppler estimada');
legend("PRN " + string(detectedPRNs), 'Location', 'best');

subplot(2,1,2);
plot(cn0History); ylabel('C/N₀ estimado (dB-Hz)');
xlabel('Bloco de tempo');
title('Estimativa indireta de C/N₀');
legend("PRN " + string(detectedPRNs), 'Location', 'best');
