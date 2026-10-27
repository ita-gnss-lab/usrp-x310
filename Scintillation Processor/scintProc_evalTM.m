function [tm] = scintProc_evalTM(tmp,isTracked)

tm.filePos_Ns                                      = tmp.filePos_Ns;
tm.IQ                                              = tmp.corrSign;
tm.CN0_PLL                                         = tmp.lock.CN0_PLL;
tm.flPessimPLL                                     = tmp.lock.flPessimPLL;
tm.flPessimDLL                                     = tmp.lock.flPessimDLL;
tm.flPessimIQbal                                   = tmp.lock.flPessimIQbal;
if isTracked
tm.phNCOScint                                      = tmp.carr.phNCOScint;
end
tm.flPessimPLL(isnan(tm.flPessimPLL))         = 0;
tm.flPessimDLL(isnan(tm.flPessimDLL))         = 0;
tm.flPessimIQbal(isnan(tm.flPessimIQbal))     = 0;


end