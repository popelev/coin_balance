import datetime
import time
import logging
from abc import ABC, abstractmethod
from typing import Dict, Tuple, Optional, Callable, List, Iterable
from requests import ReadTimeout

from web3 import AsyncWeb3
from web3.contract import AsyncContract
from web3.datastructures import AttributeDict
from web3.exceptions import BlockNotFound
from eth_abi.codec import ABICodec

# Currently this method is not exposed over official web3 API,
# but we need it to construct eth_get_logs parameters
from web3._utils.filters import construct_event_filter_params
from web3._utils.events import get_event_data

import asyncio

import numpy as n

from hexbytes import HexBytes

logger = logging.getLogger(__name__)

MAX_CHUNK_SIZE = 1000

class EventScannerState(ABC):
    """Application state that remembers what blocks we have scanned in the case of crash.
    """

    @abstractmethod
    def get_last_scanned_block(self) -> int:
        """Number of the last block we have scanned on the previous cycle.

        :return: 0 if no blocks scanned yet
        """

    @abstractmethod
    def end_chunk(self, block_number: int):
        """Scanner finished a number of blocks.

        Persistent any data in your state now.
        """

    @abstractmethod
    def process_event(self, block_when: datetime.datetime, event: AttributeDict) -> object:
        """Process incoming events.

        This function takes raw events from Web3, transforms them to your application internal
        format, then saves them in a database or some other state.

        :param block_when: When this block was mined

        :param event: Symbolic dictionary of the event data

        :return: Internal state structure that is the result of event tranformation.
        """


class EventScanner:
    """Scan blockchain for events and try not to abuse JSON-RPC API too much.

    Can be used for real-time scans, as it detects minor chain reorganisation and rescans.
    Unlike the easy web3.contract.Contract, this scanner can scan events from multiple contracts at once.
    For example, you can get all transfers from all tokens in the same scan.

    You *should* disable the default `http_retry_request_middleware` on your provider for Web3,
    because it cannot correctly throttle and decrease the `eth_get_logs` block number range.
    """

    def __init__(self, mongo, web3: AsyncWeb3, contract: AsyncContract, state: EventScannerState, events: List, filters: Dict,
                 max_chunk_scan_size: int = 10000, max_request_retries: int = 4, request_retry_seconds: float = 12.0):
        """
        :param contract: Contract
        :param events: List of web3 Event we scan
        :param filters: Filters passed to get_logs
        :param max_chunk_scan_size: JSON-RPC API limit in the number of blocks we query. (Recommendation: 10,000 for mainnet, 500,000 for testnets)
        :param max_request_retries: How many times we try to reattempt a failed JSON-RPC call
        :param request_retry_seconds: Delay between failed requests to let JSON-RPC server to recover
        """

        self.logger = logger
        self.contract = contract
        self.web3 = web3
        self.mongo = mongo

        self.state = state
        self.events = events
        self.filters = filters

        # Our JSON-RPC throttling parameters
        self.min_scan_chunk_size = 10  # 12 s/block = 120 seconds period
        self.max_scan_chunk_size = max_chunk_scan_size
        self.max_request_retries = max_request_retries
        self.request_retry_seconds = request_retry_seconds

        # Factor how fast we increase the chunk size if results are found
        # # (slow down scan after starting to get hits)
        self.chunk_size_decrease = 0.5

        # Factor how was we increase chunk size if no results found
        self.chunk_size_increase = 2.0

    @property
    def address(self):
        return self.token_address

    async def get_block_timestamp(self, block_num) -> datetime.datetime:
        """Get Ethereum block timestamp"""
        try:
            block_info = await self.web3.eth.get_block(block_num)
        except BlockNotFound:
            # Block was not mined yet,
            # minor chain reorganisation?
            return None
        last_time = block_info["timestamp"]
        return datetime.datetime.utcfromtimestamp(last_time)

    def get_suggested_scan_start_block(self):
        """Get where we should start to scan for new token events.

        If there are no prior scans, start from block 1.
        Otherwise, start from the last end block minus ten blocks.
        We rescan the last ten scanned blocks in the case there were forks to avoid
        misaccounting due to minor single block works (happens once in a hour in Ethereum).
        These heurestics could be made more robust, but this is for the sake of simple reference implementation.
        """

        end_block = self.get_last_scanned_block()
        if end_block:
            return max(1, end_block - self.NUM_BLOCKS_RESCAN_FOR_FORKS)
        return 1

    async def get_suggested_scan_end_block(self):
        """Get the last mined block on Ethereum chain we are following."""
        block = await self.web3.eth.get_block('latest')
        # Do not scan all the way to the final block, as this
        # block might not be mined yet
        return block.number - 1


    def get_last_scanned_block(self) -> int:
        return self.state.get_last_scanned_block()

    async def scan_chunk(self, start_block, end_block) -> Tuple[int, datetime.datetime, list]:
        """Read and process events between to block numbers.

        Dynamically decrease the size of the chunk if the case JSON-RPC server pukes out.

        :return: tuple(actual end block number, when this block was mined, processed events)
        """

        block_timestamps = {}
        get_block_timestamp = self.get_block_timestamp

        # Cache block timestamps to reduce some RPC overhead
        # Real solution might include smarter models around block
        async def get_block_when(block_num):
            if block_num not in block_timestamps:
                block_timestamps[block_num] = await get_block_timestamp(block_num)
            return block_timestamps[block_num]

        all_processed = []

        target_event_type = self.filters["event_type"]
        filtered_events = [event_type for event_type in self.events if event_type.event_name==target_event_type]
        for event_type in filtered_events:

            # Callable that takes care of the underlying web3 call
            async def _fetch_events(_start_block, _end_block):
                return await _fetch_events_for_all_contracts(self.web3,
                                                       event_type,
                                                       self.filters,
                                                       from_block=_start_block,
                                                       to_block=_end_block)

            # Do `n` retries on `eth_get_logs`,
            # throttle down block range if needed
            end_block, events = await _retry_web3_call(
                _fetch_events,
                start_block=start_block,
                end_block=end_block,
                retries=self.max_request_retries,
                delay=self.request_retry_seconds)

            for evt in events:
                # Integer of the log index position in the block, null when its pending
                idx = evt["logIndex"]

                # We cannot avoid minor chain reorganisations, but
                # at least we must avoid blocks that are not mined yet
                assert idx is not None, "Somehow tried to scan a pending block"

                block_number = evt["blockNumber"]

                # Get UTC time when this event happened (block mined timestamp)
                # from our in-memory cache
                block_when = await get_block_when(block_number)
                logger.info(f"Processing event {evt['event']}, block:{evt['blockNumber']} TX-index :{evt['transactionIndex']} Log-index :{evt['logIndex']}")
                processed = self.state.process_event(block_when, evt)
                all_processed.append(processed)

        end_block_timestamp = await get_block_when(end_block)
        return end_block, end_block_timestamp, all_processed

    def estimate_next_chunk_size(self, current_chuck_size: int, event_found_count: int):
        """Try to figure out optimal chunk size

        Our scanner might need to scan the whole blockchain for all events

        * We want to minimize API calls over empty blocks

        * We want to make sure that one scan chunk does not try to process too many entries once, as we try to control commit buffer size and potentially asynchronous busy loop

        * Do not overload node serving JSON-RPC API by asking data for too many events at a time

        Currently Ethereum JSON-API does not have an API to tell when a first event occurred in a blockchain
        and our heuristics try to accelerate block fetching (chunk size) until we see the first event.

        These heurestics exponentially increase the scan chunk size depending on if we are seeing events or not.
        When any transfers are encountered, we are back to scanning only a few blocks at a time.
        It does not make sense to do a full chain scan starting from block 1, doing one JSON-RPC call per 20 blocks.
        """

        if event_found_count > 0:
            # When we encounter first events, reset the chunk size window
            current_chuck_size = self.min_scan_chunk_size
        else:
            current_chuck_size *= self.chunk_size_increase

        current_chuck_size = max(self.min_scan_chunk_size, current_chuck_size)
        current_chuck_size = min(self.max_scan_chunk_size, current_chuck_size)
        return int(current_chuck_size)

    async def scan(self, start_block, end_block, start_chunk_size=20) -> Tuple[
            list, int]:

        assert start_block <= end_block

        # All processed entries we got on this scan cycle
        self.all_processed = []


        async def taskCreator(self, start_b, end_b, start_chunk):
            async def asincScan(start_b, end_b, chunk_size):
                # pass
                current_block = start_b
                last_scan_duration = last_logs_found = 0
                while current_block <= end_b:
                    # Print some diagnostics to logs to try to fiddle with real world JSON-RPC API performance
                    estimated_end_block = current_block + chunk_size
                    logger.info(f"Scanning token transfers for blocks: {current_block} - {estimated_end_block}, chunk size {chunk_size}, last chunk scan took {last_scan_duration}, last logs found {last_logs_found}")

                    start = time.time()
                    actual_end_block, end_block_timestamp, new_entries = await self.scan_chunk(current_block, estimated_end_block)
                    last_logs_found = len(new_entries)
                    # Where does our current chunk scan ends - are we out of chain yet?
                    current_end = actual_end_block

                    last_scan_duration = time.time() - start

                    self.all_processed += new_entries

                    # Try to guess how many blocks to fetch over `eth_get_logs` API next time
                    chunk_size = self.estimate_next_chunk_size(
                        chunk_size, len(new_entries))
                    chunk_size = chunk_size if chunk_size<=MAX_CHUNK_SIZE else MAX_CHUNK_SIZE

                    # Set where the next chunk starts
                    current_block = current_end + 1
                    # total_chunks_scanned += 1
                    self.state.end_chunk(current_end)
                
            tasks = []
            cursor_b = start_b
            while cursor_b + MAX_CHUNK_SIZE < end_b:
                    start = cursor_b
                    stop =  cursor_b + MAX_CHUNK_SIZE - 1
                    cursor_b = cursor_b + MAX_CHUNK_SIZE
                    tasks.append(asyncio.create_task(asincScan(start, stop, start_chunk)))
            tasks.append(asyncio.create_task(asincScan(cursor_b, end_block, start_chunk)))

            await asyncio.gather(*tasks)
            print("all tasks done")
            
         
        zzz = await taskCreator(self, start_block, end_block, start_chunk_size)
        return self.all_processed


async def _retry_web3_call(func, start_block, end_block, retries, delay) -> Tuple[int, list]:
    """A custom retry loop to throttle down block range.

    If our JSON-RPC server cannot serve all incoming `eth_get_logs` in a single request,
    we retry and throttle down block range for every retry.

    For example, Go Ethereum does not indicate what is an acceptable response size.
    It just fails on the server-side with a "context was cancelled" warning.

    :param func: A callable that triggers Ethereum JSON-RPC, as func(start_block, end_block)
    :param start_block: The initial start block of the block range
    :param end_block: The initial start block of the block range
    :param retries: How many times we retry
    :param delay: Time to sleep between retries
    """
    for i in range(retries):
        try:
            return end_block, await func(start_block, end_block)
        except Exception as e:
            # Assume this is HTTPConnectionPool(host='localhost', port=8545): Read timed out. (read timeout=10)
            # from Go Ethereum. This translates to the error "context was cancelled" on the server side:
            # https://github.com/ethereum/go-ethereum/issues/20426
            if i < retries - 1:
                # Give some more verbose info than the default middleware
                logger.warning(f"Retrying events for block range {start_block} - {end_block} ({end_block-start_block}) failed with {e}, retrying in {delay} seconds")
                # Decrease the `eth_getBlocks` range
                end_block = start_block + ((end_block - start_block) // 2)
                # Let the JSON-RPC to recover e.g. from restart
                time.sleep(delay)
                continue
            else:
                logger.warning("Out of retries")
                raise

async def _fetch_events_for_all_contracts(
        web3,
        event,
        argument_filters: dict,
        from_block: int,
        to_block: int) -> Iterable:
    """Get events using eth_get_logs API.

    This method is detached from any contract instance.

    This is a stateless method, as opposed to createFilter.
    It can be safely called against nodes which do not provide `eth_newFilter` API, like Infura.
    """

    if from_block is None:
        raise TypeError(
            "Missing mandatory keyword argument to get_logs: fromBlock")

    # Currently no way to poke this using a public Web3.py API.
    # This will return raw underlying ABI JSON object for the event
    abi = event._get_event_abi()

    # Depending on the Solidity version used to compile
    # the contract that uses the ABI,
    # it might have Solidity ABI encoding v1 or v2.
    # We just assume the default that you set on Web3 object here.
    # More information here https://eth-abi.readthedocs.io/en/latest/index.html
    codec: ABICodec = web3.codec

    # Here we need to poke a bit into Web3 internals, as this
    # functionality is not exposed by default.
    # Construct JSON-RPC raw filter presentation based on human readable Python descriptions
    # Namely, convert event names to their keccak signatures
    # More information here:
    # https://github.com/ethereum/web3.py/blob/e176ce0793dafdd0573acc8d4b76425b6eb604ca/web3/_utils/filters.py#L71
    data_filter_set, event_filter_params = construct_event_filter_params(
        abi,
        codec,
        address=argument_filters.get("address"),
        argument_filters=argument_filters,
        fromBlock=from_block,
        toBlock=to_block
    )

    logger.info(f"Querying eth_get_logs with the following parameters: {event_filter_params}")

    # Call JSON-RPC API on your Ethereum node.
    # get_logs() returns raw AttributedDict entries
    logs = await web3.eth.get_logs(event_filter_params)

    # Convert raw binary data to Python proxy objects as described by ABI
    all_events = []
    for log in logs:
        # Convert raw JSON-RPC log result to human readable event by using ABI data
        # More information how processLog works here
        # https://github.com/ethereum/web3.py/blob/fbaf1ad11b0c7fac09ba34baff2c256cffe0a148/web3/_utils/events.py#L200
        evt = get_event_data(codec, abi, log)
        # Note: This was originally yield,
        # but deferring the timeout exception caused the throttle logic not to work
        all_events.append(evt)
    return all_events

count_of_iteration = 0

async def get_contract_creation_block(web3, contract_address, blocknumber_from, blocknumber_to, count=0):
        logger.info("try to finde creation block ...")
        middle_block = (blocknumber_from + blocknumber_to) // 2
        n_minus_one_is_contract = str((await web3.eth.get_code(contract_address, middle_block-1)).hex()) != str(HexBytes('0x').hex())
        n_is_contract = str((await web3.eth.get_code(contract_address, middle_block)).hex()) != str(HexBytes('0x').hex())

        if n_is_contract and not n_minus_one_is_contract:
            logger.info(count)
            return middle_block
        elif n_is_contract and n_minus_one_is_contract:       
            return await get_contract_creation_block(web3, contract_address, blocknumber_from, middle_block, count+1)
        else:
            return await get_contract_creation_block(web3, contract_address, middle_block, blocknumber_to, count+1)

async def filter(mongo, web3, contract_address):
    # import sys
    import json

    TARGET_TOKEN_ADDRESS = contract_address

    # Reduced ERC-20 ABI, only Transfer event
    ABI = """[
        {
            "anonymous": false,
            "inputs": [
                {
                    "indexed": true,
                    "name": "from",
                    "type": "address"
                },
                {
                    "indexed": true,
                    "name": "to",
                    "type": "address"
                },
                {
                    "indexed": false,
                    "name": "value",
                    "type": "uint256"
                }
            ],
            "name": "Transfer",
            "type": "event"
        }
    ]
    """

    class JSONifiedState(EventScannerState):
        """Store the state of scanned blocks and all events.

        All state is an in-memory dict.
        Simple load/store massive JSON on start up.
        """

        def __init__(self):
            self.state = None
            self.fname = "test-state.json"
            # How many second ago we saved the JSON file
            self.last_save = 0

        def reset(self):
            """Create initial state of nothing scanned."""
            self.state = {
                "last_scanned_block": 0,
                "blocks": {},
            }

        async def restore(self, contract_address):
            """Restore the last scan state from a file."""
            try:
                logger.info("search the block of contract deploy")
                last_scanned_block = await mongo.lastScannedBlock.find_one({"contract_address": contract_address})
                self.state = {
                "last_scanned_block": last_scanned_block['block_number'],
                "blocks": {},
            }
                logger.info(
                    f"Restored the state, previously {self.state['last_scanned_block']} blocks have been scanned")
            except:
                logger.info("State starting from scratch")
                self.reset()

        def save(self):
            print("TODO_save()")
            
        def get_last_scanned_block(self):
            """The number of the last block we have stored."""
            return self.state["last_scanned_block"]

        async def end_chunk(self, block_number):
            """Save at the end of each block, so we can resume in the case of a crash or CTRL+C"""

            await mongo.lastScannedBlock.find_one_and_update({"contract_address": self.filters["address"]}, {"block_number": block_number })

        def process_event(self, block_when: datetime.datetime, event: AttributeDict) -> str:
            """Record a ERC-20 transfer in our database."""
            # Events are keyed by their transaction hash and log index
            # One transaction may contain multiple events
            # and each one of those gets their own log index

            # event_name = event.event # "Transfer"
            log_index = event.logIndex  # Log index within the block
            # transaction_index = event.transactionIndex  # Transaction index within the block
            txhash = event.transactionHash.hex()  # Transaction hash
            block_number = event.blockNumber

            # Convert ERC-20 Transfer event to our internal format
            args = event["args"]
            transfer = {
                "from": args["from"],
                "to": args.to,
                "value": args.value,
                "timestamp": block_when.isoformat(),
            }

            # Create empty dict as the block that contains all transactions by txhash
            if block_number not in self.state["blocks"]:
                self.state["blocks"][block_number] = {}

            block = self.state["blocks"][block_number]
            if txhash not in block:
                # We have not yet recorded any transfers in this transaction
                # (One transaction may contain multiple events if executed by a smart contract).
                # Create a tx entry that contains all events by a log index
                self.state["blocks"][block_number][txhash] = {}

            # Record ERC-20 transfer in our database
            self.state["blocks"][block_number][txhash][log_index] = transfer

            # Return a pointer that allows us to look up this event later if needed
            return f"{block_number}-{txhash}-{log_index}"

    async def run():

        # Enable logs to the stdout.
        # DEBUG is very verbose level
        logging.basicConfig(level=logging.INFO)

        # Prepare stub ERC-20 contract object
        abi = json.loads(ABI)
        ERC20 = web3.eth.contract(abi=abi)

        # Restore/create our persistent state
        state = JSONifiedState()
        await state.restore(TARGET_TOKEN_ADDRESS)

        # chain_id: int, web3: Web3, abi: dict, state: EventScannerState, events: List, filters: {}, max_chunk_scan_size: int=10000
        scanner = EventScanner(
            mongo=mongo,
            web3=web3,
            contract=ERC20,
            state=state,
            events=[ERC20.events.Transfer],
            filters={
                "address": TARGET_TOKEN_ADDRESS,
                "event_type" : "Transfer"
            },
            # How many maximum blocks at the time we request from JSON-RPC and we are unlikely to exceed the response size limit of the JSON-RPC server
            max_chunk_scan_size=MAX_CHUNK_SIZE
        )

        # Assume we might have scanned the blocks all the way to the last Ethereum block
        # that mined a few seconds before the previous scan run ended.
        # Because there might have been a minor Etherueum chain reorganisations
        # since the last scan ended, we need to discard
        # the last few blocks from the previous scan results.
        
        # chain_reorg_safety_blocks = 10
        # scanner.delete_potentially_forked_block_data(
        #     state.get_last_scanned_block() - chain_reorg_safety_blocks)

        # Scan from [last block scanned] - [latest ethereum block]
        # Note that our chain reorg safety blocks cannot go negative
        latest_block_t = await scanner.web3.eth.get_block('latest')
        latest_block = latest_block_t.number
        cutoff_block = state.state['last_scanned_block']
        if(cutoff_block == 0):
            cutoff_block = await get_contract_creation_block(web3, TARGET_TOKEN_ADDRESS, 1, latest_block)
            await mongo.lastScannedBlock.insert_one({"contract_address": contract_address, "block_number": cutoff_block})
        
        start = time.time()
        duration = time.time() - start

        end_block = await scanner.get_suggested_scan_end_block()
        blocks_to_scan = end_block - cutoff_block

        ts_cb = await scanner.web3.eth.get_block(cutoff_block)
        ts_eb = await scanner.web3.eth.get_block(end_block)
        logger.info(f"Scanning events from block {cutoff_block} ({datetime.datetime.fromtimestamp(ts_cb.timestamp)}) - {end_block} ({datetime.datetime.fromtimestamp(ts_eb.timestamp)}), blocks_to_scan: {blocks_to_scan}. Scan envelop estimation took {duration} seconds")

        start = time.time()

        await scanner.scan(cutoff_block, cutoff_block + MAX_CHUNK_SIZE*5+1)

        result = scanner.all_processed

        state.save()
        
        duration = time.time() - start
        logger.info(f"Scanned total {len(result)} Transfer events, in {duration} seconds")
        
        json_file_after = { "events" : []}
        data = state.state

        for block_number in data["blocks"]:
            for tx_hash in data["blocks"][block_number]:
                    for event_number in data["blocks"][block_number][tx_hash]:
                        addr_from = data["blocks"][block_number][tx_hash][event_number]["from"]
                        addr_to = data["blocks"][block_number][tx_hash][event_number]["to"]
                        value = data["blocks"][block_number][tx_hash][event_number]["value"]
                        json_file_after["events"].append(
                            {'block': block_number, 
                                'tx_hash': str(tx_hash),
                                'address_from': str(addr_from),
                                'address_to': str(addr_to),
                                'value': str(value)
                                })
                        
        if json_file_after['events'] != []:
            await mongo.transferEvents.insert_many(json_file_after['events'])

        return True
    
    success = await run()
    return success
