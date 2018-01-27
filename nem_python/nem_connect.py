#!/user/env python3
# -*- coding: utf-8 -*-

import queue
import logging
import threading
import random
import time
import requests
import json
import copy
import os
from tempfile import gettempdir
from binascii import hexlify
from .dict_math import DictMath
from .transaction_reform import TransactionReform

F_DEBUG = False
LOCAL_NIS_URL = ("http", "127.0.0.1", 7890)  # transaction_prepareでのみ使用(Debug用)
ALLOW_NIS_VER = ("0.6.93-BETA", "0.6.95-BETA")  # 使用するNISのVersion
ALLOW_DIFF_HEIGHT = 2  # 許容するHeightのズレ
ALLOW_MARGIN_EXP = 5  # NISの経験値？
TMP_DIR = ""
PEER_FILE = ""
NIS_PEERS_SET = set()  # Nisの接続先

logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


class NemConnect:
    TRANSFER_INCOMING = 'account/transfers/incoming'
    TRANSFER_OUTGOING = 'account/transfers/outgoing'
    TRANSFER_ALL = 'account/transfers/all'
    ns2def_cashe = dict()
    nem_xem_define = {
        "creator": "111112222233333a62f894e591478caa23b06f90471e7976c30fb95efda4b312",
        "description": "XEM",
        "id": {"namespaceId": "nem", "name": "xem"},
        "properties": [
            {"name": "divisibility", "value": "6"},
            {"name": "initialSupply", "value": "8999999999"},
            {"name": "supplyMutable", "value": "false"},
            {"name": "transferable", "value": "true"}],
        "levy": {}}
    timeout = 10
    f_peer_update = False
    unconfirmed_multisig_que = queue.LifoQueue(maxsize=40)
    new_received_que = queue.LifoQueue(maxsize=40)
    monitor_cks = list()  # 監視対象CompressedKey
    height = 0  # 現在のBlock高
    finish = False

    def __init__(self, main_net=True):
        self.main_net = main_net
        global TMP_DIR, PEER_FILE
        # tmpファイルの存在を確認、作成
        if main_net:
            TMP_DIR = gettempdir().replace("\\", "/") + '/nem_python'
            PEER_FILE = TMP_DIR + '/peer.json'
            if not os.path.isdir(TMP_DIR):
                os.mkdir(TMP_DIR)
        else:
            TMP_DIR = gettempdir().replace("\\", "/") + '/nem_python_test'
            PEER_FILE = TMP_DIR + '/peer.json'
            if not os.path.isdir(TMP_DIR):
                os.mkdir(TMP_DIR)
        # Lockファイルを作成
        self.lock = threading.Lock()
        # Peerを内部に保存
        original_peers = self._tmp_read(path=PEER_FILE, pre=list())
        if len(original_peers) > 5:
            NIS_PEERS_SET.update({tuple(n) for n in original_peers})
        elif main_net:
            NIS_PEERS_SET.update({
                ('http', '62.75.251.134', 7890),  # Hi, I am Alice2
                ('http', '62.75.163.236', 7890),  # Hi, I am Alice3
                ('http', '209.126.98.204', 7890),  # Hi, I am Alice4
                ('http', '108.61.182.27', 7890),  # Hi, I am Alice5
                ('http', '27.134.245.213', 7890),  # nem4ever
                ('http', '104.168.152.37', 7890),  # Phatty
            })
        else:
            NIS_PEERS_SET.update({
                ('http', '150.95.145.157', 7890),  # nis-testnet.44uk.net
                ('http', '104.128.226.60', 7890),  # Hi, I am BigAlice2
                ('http', '80.93.182.146', 7890),  # hxr.team
                ('http', '23.228.67.85', 7890),  # Hi, I am MedAlice2
                ('http', '82.196.9.187', 7890),  # NEMventory
                ('http', '188.166.14.34', 7890),  # testnet.hxr.team
            })

    def stop(self):
        while True:
            time.sleep(5)
            if not self.f_peer_update:
                self.finish = True
                break

    def _tmp_read(self, path, pre=None):
        with self.lock:
            try:
                with open(path, mode='r') as f:
                    return json.load(f)
            except FileNotFoundError:
                with open(path, mode='w+') as f:
                    json.dump(pre, f)
                    return pre

    def _tmp_write(self, path, data):
        with self.lock:
            with open(path, mode='w') as f:
                if type(data) == set:
                    json.dump(list(data), f)
                else:
                    json.dump(data, f)

    @staticmethod
    def _random_choice():
        return random.choice(list(NIS_PEERS_SET))

    def start(self):
        # Peerリスト自動更新
        def nem_peer_update():
            while True:
                self.f_peer_update = True
                self.timeout = 3
                if len(NIS_PEERS_SET) < 5 or time.time() - os.stat(PEER_FILE).st_mtime > 3600 * 3:
                    # debugﾓｰﾄﾞでないか、3時間以上更新されていない場合、Peerを更新
                    self._update_peers()
                self.f_peer_update = False
                self.timeout = 10
                time.sleep(3600 * random.random())

        # マルチシグ署名依頼
        # unconfirmed_multisig_que.get()で取得
        def unconfirmed_multisig_check():
            find_tx_list = list()
            monitor_cks = list()
            while True:
                time.sleep(5)
                if self.monitor_cks != monitor_cks:
                    monitor_cks = copy.copy(self.monitor_cks)

                try:
                    for ck in monitor_cks:
                        un = self._get_auto(
                            call="account/unconfirmedTransactions",
                            data={'address': ck.decode()})
                        for tx in un.json()['data'][::-1]:
                            if 'otherTrans' not in tx['transaction']:
                                continue  # not multisig
                            elif tx['transaction']['otherTrans'] not in find_tx_list:
                                # get new multisig transaction
                                find_tx_list.append(tx['transaction']['otherTrans'])
                                account_info = self.get_account_info(ck=ck)
                                if 'cosignatoriesCount' not in account_info['account']['multisigInfo']:
                                    # Not multisig account, may as cosigner
                                    continue
                                all_cosigner = [u['address'] for u in account_info['meta']['cosignatories']]
                                self.unconfirmed_multisig_que.put({
                                    "type": "new",
                                    "tx_hash": tx['meta']['data'],
                                    "account_ck": ck.decode(),
                                    "inner_tx": tx['transaction']['otherTrans'],
                                    "all_cosigner": all_cosigner,
                                    "need_cosigner": account_info['account']['multisigInfo']['minCosignatories'],
                                })
                                logging.info("new multisig %s %s" % (ck.decode(), tx['meta']['data']))

                            else:
                                for sign in tx['transaction']['signatures']:
                                    if sign in find_tx_list:
                                        continue
                                    else:
                                        # new cosigner transaction
                                        find_tx_list.append(sign)
                                        self.unconfirmed_multisig_que.put({
                                            "type": "cosigner",
                                            "tx_hash": tx['meta']['data'],
                                            "account_ck": ck.decode(),
                                            "inner_tx": tx['transaction']['otherTrans'],
                                            "cosigner": sign['otherAccount']})
                                        logging.info("new cosigner %s %s" % (ck.decode(), sign['otherAccount']))
                        else:
                            if len(find_tx_list) > len(monitor_cks) * 50:
                                # Remove old tx list
                                find_tx_list = find_tx_list[10:]

                except queue.Full:
                    for dummy in range(40):
                        self.unconfirmed_multisig_que.get()
                        logging.info("refresh queue")
                except Exception as e:
                    logging.debug(e)

        # 新着入金を取得
        # new_received_que.get()で取得
        def new_received_check():
            find_tx_list = list()
            monitor_cks = list()
            height = 0
            reform_obj = TransactionReform(main_net=self.main_net)
            while True:
                time.sleep(5)
                try:
                    # 新規のアカウントのみ初期化(初期化)
                    for ck in set(self.monitor_cks) - set(monitor_cks):
                        new_income = self.get_account_transfer_newest(ck=ck, call_name=self.TRANSFER_INCOMING)
                        tx_reformed = reform_obj.reform_transactions(tx_list=new_income)[::-1]
                        for tx in tx_reformed:
                            if tx not in find_tx_list:
                                find_tx_list.append(tx)
                        height = max(height, tx_reformed[-1]['height'])
                    else:
                        monitor_cks = copy.copy(self.monitor_cks)

                    # モニタリング
                    for ck in monitor_cks:
                        new_income = self.get_account_transfer_newest(ck=ck, call_name=self.TRANSFER_INCOMING)
                        tx_reformed = reform_obj.reform_transactions(tx_list=new_income)[::-1]
                        for tx in tx_reformed:
                            if tx in find_tx_list:
                                # 既に通知済み
                                continue
                            elif height > tx['height']:
                                # 前記録時より古い
                                continue
                            else:
                                height = tx['height']
                                find_tx_list.append(tx)
                                logging.info("New income tx %s" % tx['txhash'])
                                self.new_received_que.put(tx)

                    else:
                        if len(find_tx_list) > len(monitor_cks) * 50:
                            find_tx_list = find_tx_list[10:]

                except queue.Full:
                    for dummy in range(40):
                        self.new_received_que.get()
                    logging.info("refresh queue")
                except Exception as e:
                    logging.debug(e)

        # Block高の更新
        def new_block_check():
            prev_hash = ""
            height = 0
            while True:
                time.sleep(5)
                try:
                    block_data = self.get_last_chain()
                    new_prev_hash = block_data['prevBlockHash']['data']
                    new_height = block_data['height']
                    if prev_hash == new_prev_hash:
                        continue
                    elif height >= new_height:
                        continue
                    else:
                        prev_hash, height = new_prev_hash, new_height
                        self.height = new_height

                except KeyError:
                    continue
                except Exception as e:
                    logging.debug(e)

        threading.Thread(
            target=nem_peer_update, name="PeerUpdate", daemon=True
        ).start()
        threading.Thread(
            target=unconfirmed_multisig_check, name="MultisigCheck", daemon=True
        ).start()
        threading.Thread(
            target=new_received_check, name="ReceiveCheck", daemon=True
        ).start()
        threading.Thread(
            target=new_block_check, name="HeightCheck", daemon=True
        ).start()
        logging.info("start")

    def _update_peers(self):
        logging.info("update start")
        retry = 10
        while retry > 0:
            try:
                # 隣接ノードを取得
                check_url = self._random_choice()
                raw_peers = self._get(call="node/peer-list/reachable", url=check_url)
            except Exception as e:
                logging.debug(e)
                continue
            if not raw_peers.ok:
                retry -= 1
                continue
            try:
                best_height = self._get(call="/node/active-peers/max-chain-height", url=check_url)
                if not best_height.ok:
                    retry -= 1
                    continue
            except Exception as e:
                logging.debug(e)
                continue
            best_height = best_height.json()['height']
            lock = threading.Lock()
            peers = raw_peers.json()['data']
            logging.info("get raw peer: %d" % len(peers))

            # ノードリストの品質チェック
            result = list()
            thread_obj = list()
            for dummy in range(50):
                thread_obj.append(threading.Thread(
                    target=self._check_peer, args=(peers, result, lock, best_height), daemon=True
                ))
            for t in thread_obj:
                t.start()
            for t in thread_obj:
                t.join()
            logging.info("finish peer list: %d" % len(result))

            # ノードリストの更新
            with self.lock:
                NIS_PEERS_SET.update(result)
            self._tmp_write(path=PEER_FILE, data=NIS_PEERS_SET)
            return
        else:
            raise Exception("failed to update peers")

    def _check_peer(self, peers, result, lock, best_height):
        network_id = 104 if self.main_net else -104
        while True:
            with lock:
                if len(peers) > 0:
                    peer_data = peers.pop(0)
                    check_url = (
                        peer_data['endpoint']['protocol'],
                        peer_data['endpoint']['host'],
                        peer_data['endpoint']['port'])
                else:
                    break

            # meta data check
            if peer_data['metaData']['version'] not in ALLOW_NIS_VER:
                continue
            if peer_data['metaData']['networkId'] != network_id:
                continue

            try:
                # status check
                status = self._get(call="status", url=check_url)
                if not status.ok or status.json()['code'] != 6:
                    continue

                # block height check
                height = self._get(call="chain/height", url=check_url)
                if not height.ok or abs(height.json()['height'] - best_height) > ALLOW_DIFF_HEIGHT:
                    continue

                # The number of selected as partner
                experiences = self._get(call="node/experiences", url=check_url)
                if not experiences.ok or len(experiences.json()['data']) < ALLOW_MARGIN_EXP:
                    continue

                # insert good peer
                with lock:
                    result.append(check_url)
            except Exception as e:
                if F_DEBUG:
                    logging.debug("%s, %s" % (check_url[1], e))
        return

    """ rest api methods """
    def get_peers(self):
        original_peers = self._tmp_read(path=PEER_FILE, pre=list())
        NIS_PEERS_SET.update({tuple(n) for n in original_peers})
        self._tmp_write(path=PEER_FILE, data=NIS_PEERS_SET)
        return NIS_PEERS_SET

    def get_account_info(self, ck):
        """
        http://62.75.163.236:7890/account/get?address=NCR2CQE6AI3DIRHPHEPBSVDBOQFSHXFSQF4NIUAH
        meta
            cosignatories	[]
            cosignatoryOf	[]
            status	"LOCKED"
            remoteStatus	"INACTIVE"
        account
            address	"NCR2CQE6AI3DIRHPHEPBSVDBOQFSHXFSQF4NIUAH"
            harvestedBlocks	23
            balance	107987430812
            importance	0.000021955560425159027
            vestedBalance	103253738481
            publicKey	"a7d9eec00e192cdb82df471a7804974c85ba282f7f4272ec5a5dc8f640f267d3"
            label	null
            multisigInfo	{}
        """
        data = self._get_auto(
            call="account/get",
            data={"address": ck.decode()})
        if not data.ok:
            raise Exception("failed 'account/get' %s" % ck)
        return data.json()

    def get_account_owned_mosaic(self, ck):
        """
        http://62.75.163.236:7890/account/mosaic/owned?address=NCR2CQE6AI3DIRHPHEPBSVDBOQFSHXFSQF4NIUAH
        {"nem:xem": 999717,
        "gox:gox": 2,
        anko:dogfood": 20}
        """
        data = self._get_auto(
            call="account/mosaic/owned",
            data={"address": ck.decode()})
        if not data.ok:
            raise Exception("failed 'account/mosaic/owned' %s" % ck)
        return {
            "{}:{}".format(e['mosaicId']['namespaceId'], e['mosaicId']['name']): e['quantity']
            for e in data.json()['data']}

    def get_namespace2definition(self, namespace, cashe=True):
        if namespace == 'nem':
            return {'nem:xem': self.nem_xem_define}
        if namespace in self.ns2def_cashe and cashe:
            return self.ns2def_cashe[namespace]

        index_id = None
        url = self._random_choice()
        result = dict()
        while True:
            data = self._get(
                call="namespace/mosaic/definition/page",
                url=url,
                data={"namespace": namespace, "id": index_id} if index_id else {"namespace": namespace}
            )
            if not data.ok:
                index_id = None
                url = self._random_choice()
                logging.error("failed get mosaic def, retry")
                continue
            elif len(data.json()['data']) == 0:
                self.ns2def_cashe[namespace] = result
                return result
            else:
                tmp = {"{}:{}".format(e['mosaic']['id']['namespaceId'], e['mosaic']['id']['name'])
                       : e['mosaic'] for e in data.json()['data']}
                index_id = data.json()['data'][-1]['meta']['id']
                result.update(tmp)
                continue

    def get_mosaic_supply(self, namespace_name):
        data = self._get_auto(
            call='mosaic/supply',
            data={'mosaicId': namespace_name})
        if not data.ok:
            raise Exception("failed 'mosaic/supply' %s" % data.json()['message'])
        return data.json()['supply']

    def get_account_transfer_newest(self, ck, call_name=TRANSFER_INCOMING):
        """
        # account/transfers/incoming
        # account/transfers/outgoing
        # account/transfers/all
        meta
            innerHash	{}
            id	131997
            hash
                data	"d932cebfeb695acd78d36f4cbc93fd41f54a7e79123b25e97cdb301dbcb88b7f"
            height	1110957
        transaction
            timeStamp	77367540
            amount	1000000
            signature	"f7e274884fb6b826a498fb9e61bb16a4a42744b857cdba5087ed99b002f3dfce6622ea06a15c26bf8670f6dc92df3baddf7fe178e8f437563c735553cfe3b40d"
            fee	200000
            recipient	"TA4UCC6F4KVGWVPWUH6QXL2MTO4DTT2MTGWSBYCM"
            mosaics
                0
                    quantity	10
                    mosaicId
                    namespaceId	"mogamin"
                    name	"mogamin"
                1
                    quantity	10
                    mosaicId
                    namespaceId	"namutest"
                    name	"daikichi"
                2
                    quantity	100000000
                    mosaicId
                    namespaceId	"nem"
                    name	"xem"
            type	257
            deadline	77371140
            message
                payload	"74657374"
                type	1
            version	-1744830462
            signer	"47900452f5843f391e6485c5172b0e1332bf0032b04c4abe23822754214caec3"
        """
        data = self._get_auto(
            call=call_name,
            data={'address': ck.decode()})
        if not data.ok:
            raise Exception("failed '%s' %s" % (call_name, data.json()['message']))
        return data.json()['data']

    def get_account_transfer_all(self, ck, call_name=TRANSFER_INCOMING, c=100):
        """
        # account/transfers/incoming
        # account/transfers/outgoing
        # account/transfers/all
        """
        # tmpファイルの存在確認
        path = TMP_DIR + '/' + call_name.replace('/', '.') + '.' + ck.decode() + '.json'
        cashe = self._tmp_read(path=path, pre=list())
        # 履歴を取得
        while True:
            url = self._random_choice()
            data = self._get(
                call=call_name,
                url=url,
                data={'address': ck.decode()})
            if not data.ok:
                raise Exception("failed '%s' %s" % (call_name, data.json()['message']))
            j = data.json()['data']

            # TXがまだ存在しない場合
            if len(j) == 0:
                return list()
            # Casheを使用する場合
            if len(cashe) > 0:
                oldest_tx = j[-1]['transaction']
                newest_tx = j[0]['transaction']
                if newest_tx == cashe[0]['transaction']:
                    return cashe
                else:
                    for i in range(min(25, len(cashe))):
                        if oldest_tx == cashe[i]['transaction']:
                            result = j + cashe[i + 1:]
                            self._tmp_write(path=path, data=result)
                            return result

            # 同一の物がない場合
            result = j
            page_index = j[-1]['meta']['id']
            while c > 0:
                c -= 1
                data = self._get(
                    call=call_name,
                    url=url,
                    data={'address': ck.decode(), 'id': page_index})
                if not data.ok:
                    # ここはDDOS防止機構とどう付き合うか考えもの
                    raise Exception("failed '%s' %s" % (call_name, data.json()['message']))
                j = data.json()['data']

                if len(j) == 0:
                    self._tmp_write(path=path, data=result)
                    return result
                result.extend(j)
                page_index = j[-1]['meta']['id']
            else:
                logging.error("not completed! %s" % ck.decode())
                return result

    def get_account_harvests_newest(self, ck):
        data = self._get_auto(
            call="account/harvests",
            data={'address': ck.decode()})
        if not data.ok:
            raise Exception("failed 'account/harvests' %s" % data.json()['message'])
        return data.json()['data']

    def get_account_harvests_all(self, ck, c=100):
        # tmpファイルの存在確認
        path = TMP_DIR + '/' + 'account.harvests.' + '.' + ck.decode() + '.json'
        cashe = self._tmp_read(path=path, pre=list())
        # 履歴を取得
        while True:
            url = self._random_choice()
            data = self._get(
                call="account/harvests",
                url=url,
                data={'address': ck.decode()})
            if not data.ok:
                raise Exception("failed 'account/harvests' %s" % data.json()['message'])
            j = data.json()['data']

            # TXがまだ存在しない場合
            if len(j) == 0:
                return list()
            # Casheを使用する場合
            if len(cashe) > 0:
                oldest_tx = j[-1]['height']
                newest_tx = j[0]['height']
                if newest_tx == cashe[0]['height']:
                    return cashe
                else:
                    for i in range(min(25, len(cashe))):
                        if oldest_tx == cashe[i]['height']:
                            result = j + cashe[i + 1:]
                            self._tmp_write(path=path, data=result)
                            return result

            # 同一の物がない場合
            result = j
            page_index = j[-1]['id']
            while c > 0:
                c -= 1
                data = self._get(
                    call="account/harvests",
                    url=url,
                    data={'address': ck.decode(), 'id': page_index})
                if not data.ok:
                    # ここはDDOS防止機構とどう付き合うか考えもの
                    raise Exception("failed 'account/harvests' %s" % data.json()['message'])
                j = data.json()['data']

                if len(j) == 0:
                    self._tmp_write(path=path, data=result)
                    return result
                result.extend(j)
                page_index = j[-1]['id']
            else:
                logging.error("not completed! %s" % ck.decode())
                return result

    def get_last_chain(self):
        data = self._get_auto(call='chain/last-block')
        return data.json()

    """ sending functions """

    def estimate_send_fee(self, mosaics, factor=20):
        if len(mosaics) == 1 and 'nem:xem' in mosaics:
            # ver1 tx
            fee = self._calc_min_xem_fee(xem_int=mosaics['nem:xem'], factor=factor)
        else:
            # ver2 tx
            fee = 0.0
            for namespace_name in mosaics:
                divi = None
                namespace, name = namespace_name.split(":")
                definition = self.get_namespace2definition(namespace=namespace)
                if namespace_name not in definition:
                    raise Exception("not found mosaic: %s" % namespace_name)
                for p in definition[namespace_name]['properties']:
                    if p['name'] == 'divisibility':
                        divi = int(p['value'])
                fee += self._calc_mosaic_fee(
                    quantity_int=mosaics[namespace_name],
                    supply=self.get_mosaic_supply(namespace_name),
                    divi=divi, factor=factor)

        logging.info("send fee: %s XEM" % fee)
        return {"nem:xem": round(fee * 1000000)}

    @staticmethod
    def estimate_msg_fee(msg, factor=20):
        if msg is None or msg is b'':
            return {"nem:xem": 0}
        assert type(msg) == bytes, "msg is not bytes"
        fee = (len(msg) // 32 + 1) * 1 / factor
        logging.info("msg fee: %sXEM" % fee)
        return {"nem:xem": round(fee * 1000000)}

    def estimate_levy_fee(self, mosaics):
        fee = {'nem:xem': 0}
        for namespace_name in mosaics:
            if namespace_name is 'nem:xem':
                continue

            namespace, name = namespace_name.split(':')
            d = self.get_namespace2definition(namespace)
            if namespace_name not in d:
                raise Exception("not found mosaic %s" % namespace_name)
            if len(d[namespace_name]['levy']) == 0:
                continue

            levy = d[namespace_name]['levy']
            levy_mosaic = "{}:{}".format(levy['mosaicId']['namespaceId'], levy['mosaicId']['name'])
            levy_fee = levy['fee'] if levy['type'] == 1 else round(levy['fee'] * mosaics[namespace_name] / 10000)
            try:
                fee[levy_mosaic] += levy_fee
            except KeyError:
                fee[levy_mosaic] = levy_fee
            continue
        else:
            return fee

    def mosaic_transfer(self, sender_pk, recipient_ck, mosaics, msg_body=b'', msg_type=1):
        transfer_type = 1 if len(mosaics) == 1 and 'nem:xem' in mosaics else 2
        transfer_version = (1744830464 if self.main_net else -1744830464) + transfer_type
        transfer_fee = DictMath.add(self.estimate_msg_fee(msg=msg_body), self.estimate_send_fee(mosaics=mosaics))
        if transfer_type == 1:
            return {
                'type': 257,
                'version': transfer_version,
                'signer': sender_pk.decode(),
                'timeStamp': int(time.time()) - 1427587585,
                'deadline': int(time.time()) - 1427587585 + 3600 * 2,
                'recipient': recipient_ck.decode(),
                'amount': mosaics['nem:xem'],
                'fee': transfer_fee['nem:xem'],
                'message': {'type': msg_type, 'payload': hexlify(msg_body).decode()}
            }
        else:
            return {
                'type': 257,
                'version': transfer_version,
                'signer': sender_pk.decode(),
                'timeStamp': int(time.time()) - 1427587585,
                'deadline': int(time.time()) - 1427587585 + 3600,
                'recipient': recipient_ck.decode(),
                'amount': 1000000,
                'fee': transfer_fee['nem:xem'],
                'message': {'type': msg_type, 'payload': hexlify(msg_body).decode()},
                'mosaics': [
                    {'mosaicId': {'namespaceId': n.split(":")[0], 'name': n.split(":")[1]}, 'quantity': mosaics[n]}
                    for n in mosaics]
            }

    def multisig_mosaics_transfer(self, cosigner_pk, multisig_pk, recipient_ck, mosaics, msg_body=b'', msg_type=1):
        inner_transaction = self.mosaic_transfer(
            sender_pk=multisig_pk, recipient_ck=recipient_ck, mosaics=mosaics, msg_body=msg_body, msg_type=msg_type
        )
        return self._multisig_wrapper(
            cosigner_pk=cosigner_pk, inner_transaction=inner_transaction
        )

    def multisig_account_creation(self, multisig_pk, cosigner_pks, cosigner_require=0):
        tx_type = 2 if cosigner_require != 0 else 1
        tx_version = (1744830464 if self.main_net else -1744830464) + tx_type
        # cosigner_require=0 need all cosigner
        return {
            'type': 4097,
            'version': tx_version,
            'signer': multisig_pk.decode(),
            'timeStamp': int(time.time()) - 1427587585,
            'deadline': int(time.time()) - 1427587585 + 3600 * 2,
            'fee': round(0.5 * 1000000),
            'modifications': [{'modificationType': 1, 'cosignatoryAccount': p.decode()} for p in cosigner_pks],
            'minCosignatories': {'relativeChange': cosigner_require}
        }

    def multisig_account_modification(self, cosigner_pk, multisig_pk, add_pk=None, remove_pk=None, cosigner_change=0):
        tx_type = 2 if cosigner_change != 0 else 1
        tx_version = (1744830464 if self.main_net else -1744830464) + tx_type
        modifications = list()
        if add_pk:
            for p in add_pk:
                modifications.append({'modificationType': 1, 'cosignatoryAccount': p.decode()})
        if remove_pk:
            for p in remove_pk:
                modifications.append({'modificationType': 2, 'cosignatoryAccount': p.decode()})
        inner_transaction = {
            "type": 4097,
            'version': tx_version,
            'signer': multisig_pk.decode(),
            'timeStamp': int(time.time()) - 1427587585,
            'deadline': int(time.time()) - 1427587585 + 3600 * 2,
            "fee": round(0.5 * 1000000),
            "modifications": modifications,
            "minCosignatories": {"relativeChange": cosigner_change}}
        return self._multisig_wrapper(
            cosigner_pk=cosigner_pk, inner_transaction=inner_transaction
        )

    def multisig_cosigner_transaction(self, cosigner_pk, multisig_ck, inner_hash):
        transfer_type = 1
        transfer_version = (1744830464 if self.main_net else -1744830464) + transfer_type
        return {
            "type": 4098,
            "version": transfer_version,
            "signer": cosigner_pk.decode(),
            "timeStamp": int(time.time()) - 1427587585,
            "deadline": int(time.time()) - 1427587585 + 3600 * 2,
            "fee": 150000,
            "otherHash": {"data": inner_hash.decode()},
            "otherAccount": multisig_ck.decode()
        }

    @staticmethod
    def _calc_mosaic_fee(quantity_int, supply, divi, factor=20):
        logging.debug("quantity=%s,supply=%s,div=%s" % (quantity_int, supply, divi))
        if supply <= 10000 and divi == 0:
            logging.debug("Small Business Mosaic: 1")
            base_fee = 1

        else:
            # quantity, sup ともに整数値
            from math import floor, log
            _supply = supply * pow(10, divi)
            a = min(25, quantity_int * 900000 / _supply)
            b = floor(0.8 * log(9000000000000000 / _supply))
            base_fee = max(1, round(a - b))
            logging.debug("Normal Mosaic: %s" % base_fee)

        # base_feeは小数点アリの数字
        return base_fee / factor

    @staticmethod
    def _calc_min_xem_fee(xem_int, factor=20):
        return min(25, max(1, int(xem_int / 1000000 / 10000) * 1)) / factor

    def _multisig_wrapper(self, cosigner_pk, inner_transaction):
        transfer_type = 1
        transfer_version = (1744830464 if self.main_net else -1744830464) + transfer_type
        return {
            'type': 4100,
            'version': transfer_version,
            'signer': cosigner_pk.decode(),
            'timeStamp': int(time.time()) - 1427587585,
            'deadline': int(time.time()) - 1427587585 + 3600 * 2,
            'fee': round(0.15 * 1000000),
            'otherTrans': inner_transaction
        }

    """ broadcast functions """
    def transaction_prepare(self, tx_dict):
        if not F_DEBUG:
            raise Exception('Need to setup local NIS for prepare method.')
        data = self._post(
            call="transaction/prepare",
            url=LOCAL_NIS_URL,
            data=tx_dict)
        if not data.ok:
            raise Exception("failed 'transaction/prepare' %s" % data.json()['message'])
        return data.json()['data'].encode('utf8')

    def transaction_announce(self, tx_hex, tx_sign):
        data = self._post(
            call="transaction/announce",
            url=self._random_choice(),
            data={'data': tx_hex.decode(),
                  'signature': tx_sign.decode()}
        )
        if not data.ok or data.json()['message'] != 'SUCCESS':
            raise Exception("failed 'transaction/announce' %s" % data.json()['message'])
        try:
            tx_hash = data.json()['innerTransactionHash']['data']  # multi sig
        except KeyError:
            tx_hash = data.json()['transactionHash']['data']  # single sig
        return tx_hash.encode('utf8')

    def transaction_announce_dev(self, tx_hex, tx_sign):
        # 送金先を３つランダムで選ぶ
        url_set = set()
        count = 10
        while count > 0:
            count -= 1
            url_set.add(self._random_choice())
            if len(url_set) >= 3:
                break

        # 送金実行(3回)
        result_message = list()
        result_txhash = list()
        for url in url_set:
            data = self._post(
                call="transaction/announce",
                url=url,
                data={'data': tx_hex.decode(),
                      'signature': tx_sign.decode()}
            )
            message = data.json()['message']
            result_message.append(message)
            if not data.ok or message != 'SUCCESS':
                continue
            try:
                tx_hash = data.json()['innerTransactionHash']['data']  # multi sig
                result_txhash.append(tx_hash)
            except KeyError:
                tx_hash = data.json()['transactionHash']['data']  # single sig
                result_txhash.append(tx_hash)

        # 送金結果
        if 'SUCCESS' in result_message and len(result_txhash) > 0:
            return result_txhash[0]
        else:
            raise Exception("failed 'transaction/announce' %s" % result_message)

    def _get(self, call, url, data=None):
        try:
            headers = {'Content-type': 'application/json'}
            uri = "%s://%s:%d/%s" % (url[0], url[1], url[2], call)
            if not self.f_peer_update:
                logging.debug("Access GET %s (%s)" % (uri, data))
            return requests.get(uri, params=data, headers=headers, timeout=self.timeout)
        except Exception as e:
            with self.lock:
                if url in NIS_PEERS_SET:
                    NIS_PEERS_SET.remove(url)
            self._tmp_write(path=PEER_FILE, data=NIS_PEERS_SET)
            raise Exception(e)

    def _get_auto(self, call, data=None):
        retry = 10
        while retry > 0:
            retry -= 1
            url = self._random_choice()
            try:
                headers = {'Content-type': 'application/json'}
                uri = "%s://%s:%d/%s" % (url[0], url[1], url[2], call)
                return requests.get(uri, params=data, headers=headers, timeout=self.timeout)
            except Exception as e:
                with self.lock:
                    if url in NIS_PEERS_SET:
                        NIS_PEERS_SET.remove(url)
                self._tmp_write(path=PEER_FILE, data=NIS_PEERS_SET)
                logging.error(e)
                continue
        else:
            raise Exception("many retry error '%s', %s" % (call, data))

    def _post(self, call, url, data=None):
        try:
            headers = {'Content-type': 'application/json'}
            uri = "%s://%s:%d/%s" % (url[0], url[1], url[2], call)
            logging.debug("Access POST %s(%s)" % (uri, data))
            return requests.post(uri, data=json.dumps(data), headers=headers, timeout=self.timeout)
        except Exception as e:
            with self.lock:
                if url in NIS_PEERS_SET:
                    NIS_PEERS_SET.remove(url)
            self._tmp_write(path=PEER_FILE, data=NIS_PEERS_SET)
            raise Exception(e)