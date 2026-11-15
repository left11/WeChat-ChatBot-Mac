package main

import (
	"bytes"
	"fmt"
	"strings"
	"testing"

	wxproto "github.com/yincongcyincong/weixin-macos/onebot/proto"
	"google.golang.org/protobuf/proto"
)

// All response fixtures are synthetic; no account or runtime data is used.
func marshalSendResponse(t *testing.T, response proto.Message) []byte {
	t.Helper()
	data, err := proto.Marshal(response)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestParseSendMsgResponseExplicitBaseResponse(t *testing.T) {
	for _, ret := range []int32{0, -6} {
		base := &wxproto.BaseResponse{Ret: proto.Int32(ret)}
		if ret != 0 {
			base.ErrMsg = &wxproto.BaseErrMsg{Msg: proto.String("synthetic server rejection")}
		}
		for _, tc := range []struct {
			name     string
			response proto.Message
		}{
			{"generic", &wxproto.SendMsgResponse{BaseResponse: base}},
			{"voice", &wxproto.SendVoiceMsgResponse{
				SenderWxid: proto.String("synthetic_sender"), ReceiverWxid: proto.String("synthetic_receiver"),
				BaseResponse: base,
			}},
		} {
			t.Run(fmt.Sprintf("%s/ret_%d", tc.name, ret), func(t *testing.T) {
				gotRet, gotMsg, err := ParseSendMsgResponse(marshalSendResponse(t, tc.response))
				if err != nil || gotRet != ret || gotMsg != base.GetErrMsg().GetMsg() {
					t.Fatalf("got (%d, %q, %v), want (%d, %q, nil)", gotRet, gotMsg, err, ret, base.GetErrMsg().GetMsg())
				}
			})
		}
	}
}

func TestParseSendMsgResponseUnverified(t *testing.T) {
	for _, tc := range []struct {
		name string
		data []byte
	}{
		{"nil", nil},
		{"empty", []byte{}},
		{"unrelated protobuf", []byte{0xb0, 0x01, 0x01}},
		{"empty generic base response", []byte{0x0a, 0x00}},
		{"empty voice base response", []byte{0x52, 0x00}},
		{"generic missing ret", marshalSendResponse(t, &wxproto.SendMsgResponse{
			BaseResponse: &wxproto.BaseResponse{ErrMsg: &wxproto.BaseErrMsg{Msg: proto.String("synthetic error")}},
		})},
		{"voice missing ret", marshalSendResponse(t, &wxproto.SendVoiceMsgResponse{
			BaseResponse: &wxproto.BaseResponse{ErrMsg: &wxproto.BaseErrMsg{Msg: proto.String("synthetic error")}},
		})},
		{"truncated base response", []byte{0x0a, 0x02, 0x08}},
		{"malformed trailing data", []byte{0x0a, 0x02, 0x08, 0x00, 0x80}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ret, _, err := ParseSendMsgResponse(tc.data)
			if err == nil || ret == 0 {
				t.Fatalf("unverified response returned ret=%d, err=%v; must fail closed", ret, err)
			}
		})
	}
}

func TestHandleBuf2RespResult(t *testing.T) {
	original := buf2RespChan
	buf2RespChan = make(chan *Buf2RespData, 2)
	t.Cleanup(func() { buf2RespChan = original })

	for _, tc := range []struct {
		name    string
		msgType string
		data    []byte
		wantErr string
	}{
		{"empty", "text", nil, "response data is empty"},
		{"missing base response", "text", []byte{0xb0, 0x01, 0x01}, "send response unverified"},
		{"missing ret", "text", []byte{0x0a, 0x00}, "send response unverified"},
		{"malformed response", "voice", []byte{0x0a, 0x02, 0x08}, "send response unverified"},
		{"generic base ret zero", "text", []byte{0x0a, 0x02, 0x08, 0x00}, ""},
		{"voice base ret zero", "voice", []byte{0x52, 0x02, 0x08, 0x00}, ""},
		{"server rejection", "text", marshalSendResponse(t, &wxproto.SendMsgResponse{
			BaseResponse: &wxproto.BaseResponse{Ret: proto.Int32(-6), ErrMsg: &wxproto.BaseErrMsg{Msg: proto.String("synthetic rejection")}},
		}), "ret=-6, errMsg=synthetic rejection"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			HandleBuf2Resp(tc.msgType, tc.data)
			select {
			case result := <-buf2RespChan:
				if result.MsgType != tc.msgType || !bytes.Equal(result.Data, tc.data) {
					t.Fatalf("response identity/data changed: %#v", result)
				}
				if tc.wantErr == "" {
					if result.Err != nil {
						t.Fatalf("explicit base return code rejected: %v", result.Err)
					}
				} else if result.Err == nil || !strings.Contains(result.Err.Error(), tc.wantErr) {
					t.Fatalf("got err=%v, want error containing %q", result.Err, tc.wantErr)
				}
			default:
				t.Fatal("no response result")
			}
			select {
			case result := <-buf2RespChan:
				t.Fatalf("duplicate result: %#v", result)
			default:
			}
		})
	}
}

func TestHandleBuf2RespAppAttachRouting(t *testing.T) {
	originalSend, originalAttach := buf2RespChan, appAttachRespChan
	buf2RespChan = make(chan *Buf2RespData, 1)
	appAttachRespChan = make(chan []byte, 1)
	t.Cleanup(func() { buf2RespChan, appAttachRespChan = originalSend, originalAttach })
	data := []byte("synthetic upload response")
	HandleBuf2Resp("appattach", data)
	select {
	case got := <-appAttachRespChan:
		if !bytes.Equal(got, data) {
			t.Fatalf("upload response changed: %q", got)
		}
	default:
		t.Fatal("upload response was not routed")
	}
	select {
	case result := <-buf2RespChan:
		t.Fatalf("upload response treated as message send: %#v", result)
	default:
	}
}
