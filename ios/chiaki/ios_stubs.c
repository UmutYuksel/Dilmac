/*
 * Dilmaç (iOS): stand-ins for the parts of libchiaki that Remote Play over the internet needs.
 *
 * chiaki-ng's holepunch.c (PSN-relayed connection) pulls in curl with WebSockets, json-c, miniupnpc and libevent,
 * and aia.c (fetching missing certificate issuers for PSN) pulls in curl. Dilmaç only connects to a PS5 on the
 * same local network and always passes holepunch_session = NULL / holepunch_info = NULL, so none of these are ever
 * reached. They are left out of the iOS build and replaced by the functions below so the library still links.
 *
 * Licensed under AGPL-3.0 like libchiaki.
 */
#include <netinet/in.h> /* INET6_ADDRSTRLEN, used by holepunch.h */

#include <chiaki/common.h>
#include <chiaki/remote/holepunch.h>

#include <string.h>

/* common.c: chiaki_lib_init() calls this. Nothing to set up without AIA. */
ChiakiErrorCode chiaki_aia_init(void) { return CHIAKI_ERR_SUCCESS; }
void chiaki_aia_fini(void) {}

CHIAKI_EXPORT ChiakiHolepunchRegistInfo chiaki_get_regist_info(ChiakiHolepunchSession session)
{
	(void)session;
	ChiakiHolepunchRegistInfo info;
	memset(&info, 0, sizeof(info));
	return info;
}

CHIAKI_EXPORT void chiaki_get_ps_selected_addr(ChiakiHolepunchSession session, char *ps_ip)
{
	(void)session;
	if(ps_ip)
		ps_ip[0] = '\0';
}

CHIAKI_EXPORT uint16_t chiaki_get_ps_ctrl_port(ChiakiHolepunchSession session)
{
	(void)session;
	return 0;
}

CHIAKI_EXPORT chiaki_socket_t *chiaki_get_holepunch_sock(ChiakiHolepunchSession session, ChiakiHolepunchPortType type)
{
	(void)session;
	(void)type;
	return NULL;
}

CHIAKI_EXPORT ChiakiErrorCode chiaki_holepunch_session_punch_hole(
		ChiakiHolepunchSession session, ChiakiHolepunchPortType port_type)
{
	(void)session;
	(void)port_type;
	return CHIAKI_ERR_UNKNOWN;
}

/* (no chiaki_ prefix upstream) */
CHIAKI_EXPORT ChiakiErrorCode holepunch_session_create_offer(ChiakiHolepunchSession session)
{
	(void)session;
	return CHIAKI_ERR_UNKNOWN;
}

CHIAKI_EXPORT void chiaki_holepunch_session_fini(ChiakiHolepunchSession session) { (void)session; }
