/*
 * Dilmaç (iOS): minimal stand-in for <curl/curl.h>.
 * chiaki-ng's common.c includes aia.h, which includes curl only for the CURL handle type in declarations that the
 * iOS build never compiles or calls (see ios_stubs.c). An opaque type is enough for those declarations to parse.
 */
#ifndef DILMAC_COMPAT_CURL_H
#define DILMAC_COMPAT_CURL_H
typedef void CURL;
#endif
