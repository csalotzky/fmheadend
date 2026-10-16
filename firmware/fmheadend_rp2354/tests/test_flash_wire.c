#include <stdio.h>
#include <string.h>
#include "host_test_frame.h"
#include "flash_update.h"
static bool environment(fu_environment_t *e) { memset(e,0,sizeof(*e));return false; }
static const fu_ops_t ops={.environment=environment};
int main(void) {
    host_protocol_t host;host_work_t work;
    flash_update_init(&ops);host_protocol_init(&host,0);
    const uint8_t body[]={FU_COMMAND,255,1,FU_STATUS};
    test_receive(&host,body,sizeof(body));host_protocol_stop(&host,0);
    assert(host.pending);assert(host_protocol_take_work(&host,&work));
    /* Early split READ preserves work; completing between reads must not
     * splice an OK payload into the BUSY frame. */
    assert(host_protocol_read(&host)==HOST_BUSY);assert(host_protocol_read(&host)==0);
    host_protocol_run_work(&host,&work,0);host_protocol_complete_work(&host,&work);
    assert(host.update_reply_deferred);
    host_protocol_read(&host);host_protocol_read(&host);
    assert(host_protocol_read(&host)==HOST_OK);assert(!host.update_reply_deferred);
    host.response_cursor=0;
    host_protocol_run_work(&host,&work,0);host_protocol_complete_work(&host,&work);
    test_response_crc(&host);assert(host_protocol_read(&host)==0);assert(host_protocol_read(&host)==FU_REPLY_SIZE);
    host_protocol_stop(&host,0);assert(host.response_cursor==2);
    for(unsigned i=2;i<FU_REPLY_SIZE+4;++i)assert(host_protocol_read(&host)==host.response[i]);
    host_protocol_stop(&host,0);assert(host.response_cursor==FU_REPLY_SIZE+4);
    /* CRC failure cannot enqueue or execute update work. */
    test_receive(&host,body,sizeof(body));host.request[4]^=1;host_protocol_stop(&host,0);
    assert(!host.pending && host.response[0]==HOST_INVALID_ARGUMENT);test_response_crc(&host);
    uint8_t bad[]={FU_COMMAND,0,1,FU_STATUS};test_receive(&host,bad,sizeof(bad));host_protocol_stop(&host,0);
    assert(!host.pending && host.response[0]==HOST_INVALID_TARGET);
    puts("FLASH_UPDATE V1: foreground handoff, split READ/STOP cursor, CRC and target rejection passed");
}
