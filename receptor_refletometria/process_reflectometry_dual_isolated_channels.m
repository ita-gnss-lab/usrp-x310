 %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
% TEST acquisition and tracking
%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
addpath('/Users/douglassilva/Documents/MATLAB/MATLAB_GNSS_GPS_LAB_ITA/generate_reference');
addpath('/Users/douglassilva/Documents/MATLAB/MATLAB_GNSS_GPS_LAB_ITA/read_data');
addpath('/Users/douglassilva/Documents/MATLAB/MATLAB_GNSS_GPS_LAB_ITA/CA_acquisition');
addpath('/Users/douglassilva/Documents/MATLAB/MATLAB_GNSS_GPS_LAB_ITA/teste_calibration');


clear all; close all;
clc


USE_PLL = true;
%parameters
fs=2e6;
T_d=1e-3;
f_seq=1.023e6;
Ts=1/fs;
Tc=1/f_seq;
f_c=1575.42e6;
num_periods=1;
num_noncoh_periods=10;
Delta=0.4*Tc;% Bandwidth dependent!!!!
K=2000;%K=600000;
N=num_periods*fs*T_d;
N_acq=fs*T_d;
index_sample_in=1;
threshold_CH1=135e-3*num_noncoh_periods;   %1.2e3*num_noncoh_periods;
threshold_CH2=100e-3*num_noncoh_periods;   %1.2e3*num_noncoh_periods;
doppler_bin_vec=-5e3:100:5e3;
S_p=Scurve_slope_noncoh_dis(fs/2,Delta,Tc); %slope delay discriminator linearization

%parameters for carrier to noise estimation
M=20;
a_ctnd=0.1;


% load signal 
load('experimento_75.mat')


%%Prepare ACQ_DATA for both channels
for index_acq=1:32
    ACQ_DATA_CH1(index_acq).cost_function=zeros(length(doppler_bin_vec),N_acq);
    ACQ_DATA_CH1(index_acq).max_index=[0,0];
    ACQ_DATA_CH1(index_acq).max_val=0;

    ACQ_DATA_CH2(index_acq).cost_function=zeros(length(doppler_bin_vec),N_acq);
    ACQ_DATA_CH2(index_acq).max_index=[0,0];
    ACQ_DATA_CH2(index_acq).max_val=0;
end

%% Run Loop

for k=1:1800

    % Sample signals

    x_CH1 = (total_acq(index_sample_in:index_sample_in+N-1,1))';
    x_CH2 = (total_acq(index_sample_in:index_sample_in+N-1,2))';

    index_sample_out = index_sample_in+N;

    if index_sample_out > size(total_acq(:,1),1)
        break;
    end

    disp(k);
    index_sample_in=index_sample_out;

    %time
    %t=((k-1)*N)*Ts:Ts:((k-1)*N+N-1)*Ts;
    t=0:Ts:(N-1)*Ts;

    if k<=num_noncoh_periods
        %acquisition

        [ACQ_DATA_CH1,sats_found_CH1]=ca_acquisition_high_perf(t(1:N_acq),x_CH1(1:N_acq),fs,f_seq,doppler_bin_vec,threshold_CH1,ACQ_DATA_CH1);
        [ACQ_DATA_CH2,sats_found_CH2]=ca_acquisition_high_perf(t(1:N_acq),x_CH2(1:N_acq),fs,f_seq,doppler_bin_vec,threshold_CH2,ACQ_DATA_CH2);

        fprintf("Sats found CH1: \n");
        disp(sats_found_CH1);
        fprintf("Sats found CH2: \n");
        disp(sats_found_CH1);

        sats_found_CH12 = intersect(sats_found_CH1, sats_found_CH2);
        sats_found_CH1 = sats_found_CH12;
        sats_found_CH2 = sats_found_CH12;
        fprintf("Sats found both channels: \n");
        disp(sats_found_CH12);
        % Check the order: 
        if ~(isequal(sats_found_CH12, sats_found_CH1) && isequal(sats_found_CH12, sats_found_CH2))
            disp('The order of elements in sats_found_CH12 DOES NOT matches sats_found_CH1 and sats_found_CH2.');
        end



        % In the last loop, we initialize the vectors based on the INTERSECTION of the sats found vector
        if k==num_noncoh_periods

            % Initialize tracking CH1

            phase_CH1=zeros(k,length(sats_found_CH1));
            phase_doppler_CH1=zeros(k,length(sats_found_CH1));
            delay_CH1=zeros(k,length(sats_found_CH1));
            doppler_CH1=zeros(k,length(sats_found_CH1));
            error_signal_delay_CH1=zeros(k,length(sats_found_CH1));
            error_signal_phase_CH1=zeros(k,length(sats_found_CH1));
            error_signal_doppler_CH1=zeros(k,length(sats_found_CH1));
            corr_early_CH1=zeros(k,length(sats_found_CH1));
            corr_late_CH1=zeros(k,length(sats_found_CH1));
            corr_prompt_CH1=zeros(k,length(sats_found_CH1));
            Q_CH1=zeros(k,length(sats_found_CH1));
            I_CH1=zeros(k,length(sats_found_CH1));
            SNR_CH1=zeros(k,length(sats_found_CH1));
            M2_CH1=zeros(k,length(sats_found_CH1));
            M4_CH1=zeros(k,length(sats_found_CH1));
            ctnd_CH1=zeros(k,length(sats_found_CH1));
            abs_values_index_CH1=zeros(k,length(sats_found_CH1));

            % Initialize tracking CH2

            phase_CH2=zeros(k,length(sats_found_CH2));
            phase_doppler_CH2=zeros(k,length(sats_found_CH2));
            delay_CH2=zeros(k,length(sats_found_CH2));
            doppler_CH2=zeros(k,length(sats_found_CH2));
            error_signal_delay_CH2=zeros(k,length(sats_found_CH2));
            error_signal_phase_CH2=zeros(k,length(sats_found_CH2));
            error_signal_doppler_CH2=zeros(k,length(sats_found_CH2));
            corr_early_CH2=zeros(k,length(sats_found_CH2));
            corr_late_CH2=zeros(k,length(sats_found_CH2));
            corr_prompt_CH2=zeros(k,length(sats_found_CH2));
            Q_CH2=zeros(k,length(sats_found_CH2));
            I_CH2=zeros(k,length(sats_found_CH2));
            SNR_CH2=zeros(k,length(sats_found_CH2));
            M2_CH2=zeros(k,length(sats_found_CH2));
            M4_CH2=zeros(k,length(sats_found_CH2));
            ctnd_CH2=zeros(k,length(sats_found_CH2));
            abs_values_index_CH2=zeros(k,length(sats_found_CH2));


            % Initialize delay and Doppler for CH1 and CH2
            for m=1:length(sats_found_CH1)
                delay_CH1(k,m)=ACQ_DATA_CH1(sats_found_CH1(m)).max_index(2)*Ts;
                doppler_CH1(k,m)=doppler_bin_vec(ACQ_DATA_CH1(sats_found_CH1(m)).max_index(1));
            end

            % Initialize delay and Doppler for CH1 and CH2
            for m=1:length(sats_found_CH2)
                delay_CH1(k,m)=ACQ_DATA_CH1(sats_found_CH2(m)).max_index(2)*Ts;
                doppler_CH1(k,m)=doppler_bin_vec(ACQ_DATA_CH2(sats_found_CH2(m)).max_index(1));
            end


        end

    else

        %DLL, PLL, FLL

        for m=1:length(sats_found_CH1)

            %doppler
            x_d_H_CH1=conj(conj(x_CH1).*exp(1i*(phase_doppler_CH1(k-1,m)+2*pi*doppler_CH1(k-1,m).*t)))';
            x_d_H_CH2=conj(conj(x_CH2).*exp(1i*(phase_doppler_CH2(k-1,m)+2*pi*doppler_CH2(k-1,m).*t)))';

            %correlators CH1
            delay_early_CH1=delay_CH1(k-1,m)+Delta;
            delay_late_CH1=delay_CH1(k-1,m)-Delta;
            delay_prompt_CH1=delay_CH1(k-1,m);

            ref_early_CH1=reference_signal(sats_found_CH1(m),delay_early_CH1,f_seq,fs,num_periods)';
            ref_late_CH1=reference_signal(sats_found_CH1(m),delay_late_CH1,f_seq,fs,num_periods)';
            ref_prompt_CH1=reference_signal(sats_found_CH1(m),delay_prompt_CH1,f_seq,fs,num_periods)';

            ref_early_CH1 = ref_early_CH1(1:size(x_d_H_CH1,1));
            ref_late_CH1 = ref_late_CH1(1:size(x_d_H_CH1,1));
            ref_prompt_CH1 = ref_prompt_CH1(1:size(x_d_H_CH1,1));

            corr_early_CH1(k-1,m)=(conj(x_d_H_CH1)'*ref_early_CH1)/N;
            corr_late_CH1(k-1,m)=(conj(x_d_H_CH1)'*ref_late_CH1)/N;
            corr_prompt_CH1(k-1,m)=(conj(x_d_H_CH1)'*ref_prompt_CH1)/N;

            %correlators CH2
            delay_early_CH2=delay_CH2(k-1,m)+Delta;
            delay_late_CH2=delay_CH2(k-1,m)-Delta;
            delay_prompt_CH2=delay_CH2(k-1,m);

            ref_early_CH2=reference_signal(sats_found_CH2(m),delay_early_CH2,f_seq,fs,num_periods)';
            ref_late_CH2=reference_signal(sats_found_CH2(m),delay_late_CH2,f_seq,fs,num_periods)';
            ref_prompt_CH2=reference_signal(sats_found_CH2(m),delay_prompt_CH2,f_seq,fs,num_periods)';

            ref_early_CH2 = ref_early_CH2(1:size(x_d_H_CH2,1));
            ref_late_CH2 = ref_late_CH2(1:size(x_d_H_CH2,1));
            ref_prompt_CH2 = ref_prompt_CH2(1:size(x_d_H_CH2,1));

            corr_early_CH2(k-1,m)=(conj(x_d_H_CH2)'*ref_early_CH2)/N;
            corr_late_CH2(k-1,m)=(conj(x_d_H_CH2)'*ref_late_CH2)/N;
            corr_prompt_CH2(k-1,m)=(conj(x_d_H_CH2)'*ref_prompt_CH2)/N;

                

            %DLL

            error_signal_delay_CH1(k-1,m)=abs(corr_early_CH1(k-1,m))^2-abs(corr_late_CH1(k-1,m))^2;
            error_signal_delay_CH2(k-1,m)=abs(corr_early_CH2(k-1,m))^2-abs(corr_late_CH2(k-1,m))^2;

            
            %first order DLL
            K_tau_CH1=1/(S_p*abs(corr_prompt_CH1(k-1,m))^2);
            K_tau_CH2=1/(S_p*abs(corr_prompt_CH2(k-1,m))^2);
            K_tau_1=0.003992;
            a_tau_0=K_tau_1;
            B_tau_1=K_tau_1/(2*(2-K_tau_1))/T_d;

            delay_correction_CH1 = K_tau_CH1*a_tau_0*error_signal_delay_CH1(k-1,m);
            delay_correction_CH2 = K_tau_CH2*a_tau_0*error_signal_delay_CH2(k-1,m);

            delay_CH1(k,m)=delay_CH1(k-1,m)+delay_correction_CH1;
            delay_CH2(k,m)=delay_CH2(k-1,m)+delay_correction_CH2;


            %PLL
            Q_CH1(k-1,m)=imag(corr_prompt_CH1(k-1,m)*exp(1i*phase_CH1(k-1,m)));
            I_CH1(k-1,m)=real(corr_prompt_CH1(k-1,m)*exp(1i*phase_CH1(k-1,m)));

            Q_CH2(k-1,m)=imag(corr_prompt_CH2(k-1,m)*exp(1i*phase_CH2(k-1,m)));
            I_CH2(k-1,m)=real(corr_prompt_CH2(k-1,m)*exp(1i*phase_CH2(k-1,m)));

            abs_values_index(k-1,m) = index_sample_out;

            error_signal_phase_CH1(k-1,m)=-atan(Q_CH1(k-1,m)/I_CH1(k-1,m));
            error_signal_phase_CH2(k-1,m)=-atan(Q_CH2(k-1,m)/I_CH2(k-1,m));


            %second order PLL
            if k<num_noncoh_periods+3
                phase_CH1(k,m)=phase_CH1(k-1,m)+0.5*error_signal_phase_CH1(k-1,m);
                phase_CH2(k,m)=phase_CH2(k-1,m)+0.5*error_signal_phase_CH2(k-1,m);

            else
                %B_phi=50 Hz, supercritically damped response, no
                %computation delay (book Betz)
                K_phi_1=0.1438;
                K_phi_2=0.005576;
                K_phi=1;
                a_phi_0=K_phi_1+K_phi_2;
                a_phi_1=K_phi_2;
                B_phi_2=((2*K_phi_1^2+2*K_phi_2+K_phi_1*K_phi_2)/(2*K_phi_1*(4-2*K_phi_1-K_phi_2)))/T_d;

                % Computes phase for both channels
                if USE_PLL
                    phase_CH1(k,m)=2*phase_CH1(k-1,m)-phase_CH1(k-2,m)+a_phi_0*K_phi*error_signal_phase_CH1(k-1,m)+K_phi*(a_phi_1-a_phi_0)*error_signal_phase_CH1(k-2,m);
                    phase_CH2(k,m)=2*phase_CH2(k-1,m)-phase_CH2(k-2,m)+a_phi_0*K_phi*error_signal_phase_CH2(k-1,m)+K_phi*(a_phi_1-a_phi_0)*error_signal_phase_CH2(k-2,m);
                else
                    phase_CH1(k,m) = phase_CH1(k-1,m) + 0.5*error_signal_phase_CH1(k-1,m);
                    phase_CH2(k,m) = phase_CH2(k-1,m) + 0.5*error_signal_phase_CH2(k-1,m);
                end

                

            end




            if k<num_noncoh_periods+4
                doppler_CH1(k,m)=doppler_CH1(k-1,m);
                error_signal_doppler_CH1(k-1,m)=0;

                doppler_CH2(k,m)=doppler_CH2(k-1,m);
                error_signal_doppler_CH2(k-1,m)=0;



            else
                %FLL

                % Computes FLL for Channel 1
                phase_1_CH1=-atan(imag(corr_prompt_CH1(k-1,m))/real(corr_prompt_CH1(k-1,m)));
                phase_2_CH1=-atan(imag(corr_prompt_CH1(k-2,m))/real(corr_prompt_CH1(k-2,m)));

                disc_nu_CH1=-atan(((imag(corr_prompt_CH1(k-1,m))*real(corr_prompt_CH1(k-2,m)))- (imag(corr_prompt_CH1(k-2,m))*real(corr_prompt_CH1(k-1,m))))/((real(corr_prompt_CH1(k-1,m))*real(corr_prompt_CH1(k-2,m)))+(imag(corr_prompt_CH1(k-1,m))*imag(corr_prompt_CH1(k-2,m)))));
            
                error_signal_doppler_CH1(k-1,m)=disc_nu_CH1/(2*pi*num_periods*T_d);

                %third order FLL
                %B_nu=5 Hz, supercritically damped response, no computation delay
                K_nu_1=0.01438;
                K_nu_2=6.941e-5;
                K_nu_3=1.118e-7;
                K_nu=1; %tuning
                a_nu_0=(K_nu_1+K_nu_2+K_nu_3);
                a_nu_1=(K_nu_2+2*K_nu_3);
                a_nu_2= K_nu_3;
                doppler_CH1(k,m)=3*doppler_CH1(k-1,m)-3*doppler_CH1(k-2,m)+doppler_CH1(k-3,m)+a_nu_0*K_nu*error_signal_doppler_CH1(k-1,m)+K_nu*(a_nu_1-2*a_nu_0)*error_signal_doppler_CH1(k-2,m)+K_nu*(a_nu_0-a_nu_1)*error_signal_doppler_CH1(k-3,m);
                
                % Computes FLL for Channel 1
                phase_1_CH2=-atan(imag(corr_prompt_CH2(k-1,m))/real(corr_prompt_CH2(k-1,m)));
                phase_2_CH2=-atan(imag(corr_prompt_CH2(k-2,m))/real(corr_prompt_CH2(k-2,m)));

                disc_nu_CH2=-atan(((imag(corr_prompt_CH2(k-1,m))*real(corr_prompt_CH2(k-2,m)))- (imag(corr_prompt_CH2(k-2,m))*real(corr_prompt_CH2(k-1,m))))/((real(corr_prompt_CH2(k-1,m))*real(corr_prompt_CH2(k-2,m)))+(imag(corr_prompt_CH2(k-1,m))*imag(corr_prompt_CH2(k-2,m)))));

                error_signal_doppler_CH2(k-1,m)=disc_nu_CH2/(2*pi*num_periods*T_d);

                %third order FLL
                %B_nu=5 Hz, supercritically damped response, no computation delay
                K_nu_1=0.01438;
                K_nu_2=6.941e-5;
                K_nu_3=1.118e-7;
                K_nu=1; %tuning
                a_nu_0=(K_nu_1+K_nu_2+K_nu_3);
                a_nu_1=(K_nu_2+2*K_nu_3);
                a_nu_2= K_nu_3;
                doppler_CH2(k,m)=3*doppler_CH2(k-1,m)-3*doppler_CH2(k-2,m)+doppler_CH2(k-3,m)+a_nu_0*K_nu*error_signal_doppler_CH2(k-1,m)+K_nu*(a_nu_1-2*a_nu_0)*error_signal_doppler_CH2(k-2,m)+K_nu*(a_nu_0-a_nu_1)*error_signal_doppler_CH2(k-3,m);
                


            end

            %Doppler phase update
            phase_doppler_CH1(k,m)=phase_doppler_CH1(k-1,m)+2*pi*doppler_CH1(k,m)*T_d;
            phase_doppler_CH2(k,m)=phase_doppler_CH2(k-1,m)+2*pi*doppler_CH2(k,m)*T_d;

            %Carrier aiding after loop filter
            delay_CH1(k,m)=delay_CH1(k,m)+(doppler_CH1(k,m)/f_c*T_d);
            

            %C/N0 estimator, based on the signal obtained
            
            
            aux1=floor(k/M)+1;
            
            if rem(k,M)==0
                SNR_CH1(aux1,m)=sqrt(2*M2_CH1(aux1-1,m)^2-M4_CH1(aux1-1,m))/(M2_CH1(aux1-1,m)-sqrt(2*M2_CH1(aux1-1,m)^2-M4_CH1(aux1-1,m)));
                M2_CH1(aux1,m)=(abs(1i*Q_CH1(k-1,m)+I_CH1(k-1,m))^2)/M;
                M4_CH1(aux1,m)=(abs(1i*Q_CH1(k-1,m)+I_CH1(k-1,m))^4)/M;
                ctnd_CH1(k,m)=a_ctnd*real(10*log10(SNR_CH1(aux1,m))-10*log10(T_d))+(1-a_ctnd)*ctnd_CH1(k-1,m);

                SNR_CH2(aux1,m)=sqrt(2*M2_CH2(aux1-1,m)^2-M4_CH2(aux1-1,m))/(M2_CH2(aux1-1,m)-sqrt(2*M2_CH2(aux1-1,m)^2-M4_CH2(aux1-1,m)));
                M2_CH2(aux1,m)=(abs(1i*Q_CH2(k-1,m)+I_CH2(k-1,m))^2)/M;
                M4_CH2(aux1,m)=(abs(1i*Q_CH2(k-1,m)+I_CH2(k-1,m))^4)/M;
                ctnd_CH2(k,m)=a_ctnd*real(10*log10(SNR_CH2(aux1,m))-10*log10(T_d))+(1-a_ctnd)*ctnd_CH2(k-1,m);


            else
                M2_CH1(aux1,m)=M2_CH1(aux1,m)+(abs(1i*Q_CH1(k-1,m)+I_CH1(k-1,m))^2)/M;
                M4_CH1(aux1,m)=M4_CH1(aux1,m)+(abs(1i*Q_CH1(k-1,m)+I_CH1(k-1,m))^4)/M;
                ctnd_CH1(k,m)=ctnd_CH1(k-1,m);

                M2_CH2(aux1,m)=M2_CH2(aux1,m)+(abs(1i*Q_CH2(k-1,m)+I_CH2(k-1,m))^2)/M;
                M4_CH2(aux1,m)=M4_CH2(aux1,m)+(abs(1i*Q_CH2(k-1,m)+I_CH2(k-1,m))^4)/M;
                ctnd_CH2(k,m)=ctnd_CH2(k-1,m);

            end

        end



    end

end

% %% % 
% % %Plot
% %tracking error_signal
% figure('Name','Delay error','NumberTitle','off');
% plot(error_signal_delay); grid on;

% figure('Name','Delay','NumberTitle','off');
% plot(delay); grid on;

% figure('Name','Phase error','NumberTitle','off');
% plot(error_signal_phase);grid on;

% figure('Name','Carrier Phase','NumberTitle','off');
% plot(phase);grid on;

% figure('Name','Doppler error','NumberTitle','off');

% plot(error_signal_doppler);grid on;

% figure('Name','Doppler shift','NumberTitle','off');
% plot(doppler);grid on;

% figure('Name','Doppler phase','NumberTitle','off');
% plot(phase_doppler);grid on;

% figure('Name','In-Phase','NumberTitle','off');
% plot(I);grid on;

% figure('Name','Quadrature','NumberTitle','off');
% plot(Q);grid on;

% figure('Name', 'C/N0', 'NumberTitle','off');
% plot(ctnd);grid on;

% %% Plot - Legend


t_full=0:T_d:((k-1))*T_d;


%% ------- Plot CH1

figure('Name','Delay','NumberTitle','off');

subplot(1,2,1);
plot(t_full, delay_CH1); grid on;
legend(num2str(sats_found_CH1',"PRN%2d"));
xlabel("Time [s]");
ylabel("Delay [s]");
title('Delay: CH1');

subplot(1,2,2);
plot(t_full, delay_CH2); grid on;
legend(num2str(sats_found_CH2',"PRN%2d"));
xlabel("Time [s]");
ylabel("Delay [s]");
title('Delay: CH2');


figure('Name','Carrier Phase','NumberTitle','off');

subplot(1,2,1);
plot(t_full, phase_CH1);grid on;
legend(num2str(sats_found_CH1',"PRN%2d"));
xlabel("Time [s]");
ylabel("Carrier Phase [rad]");
title('Carrier Phase: CH1');

subplot(1,2,2);
plot(t_full, phase_CH2);grid on;
legend(num2str(sats_found_CH2',"PRN%2d"));
xlabel("Time [s]");
ylabel("Carrier Phase [rad]");
title('Carrier Phase: CH2');

figure('Name','Doppler Shift','NumberTitle','off');

subplot(1,2,1);
plot(t_full, doppler_CH1);grid on;
legend(num2str(sats_found_CH1',"PRN%2d"));
xlabel("Time [s]");
ylabel("Doppler Shift [Hz]");
title('Doppler Shift: CH1');

subplot(1,2,2);
plot(t_full, doppler_CH2);grid on;
legend(num2str(sats_found_CH2',"PRN%2d"));
xlabel("Time [s]");
ylabel("Doppler Shift [Hz]");
title('Doppler Shift: CH2');


figure('Name','Doppler Phase','NumberTitle','off');

subplot(1,2,1);
plot(t_full, phase_doppler_CH1);grid on;
legend(num2str(sats_found_CH1',"PRN%2d"));
xlabel("Time [s]");
ylabel("Doppler Phase [rad]");
title('Doppler Phase: CH1');

subplot(1,2,2);
plot(t_full, phase_doppler_CH2);grid on;
legend(num2str(sats_found_CH2',"PRN%2d"));
xlabel("Time [s]");
ylabel("Doppler Phase [rad]");
title('Doppler Phase: CH2');

figure('Name','C/N_0','NumberTitle','off');

subplot(1,2,1);
plot(t_full, ctnd_CH1);grid on;
legend(num2str(sats_found_CH1',"PRN%2d"));
xlabel("Time [s]");
ylabel("C/N_0 [dB]");
title('C/N_0: CH1');

subplot(1,2,2);
plot(t_full, ctnd_CH2);grid on;
legend(num2str(sats_found_CH2',"PRN%2d"));
xlabel("Time [s]");
ylabel("C/N_0 [dB]");
title('C/N_0: CH2');



%%

%save("experimento_daniele.mat")

