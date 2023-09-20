from aiohttp import web
from aiohttp.web_response import Response
from pathlib import Path
import json
import os
from langchain.chat_models import ChatOpenAI
from langchain import PromptTemplate
from langchain.chains import RetrievalQA, LLMChain
from langchain.agents import initialize_agent, Tool, AgentType
from langchain.output_parsers import PydanticOutputParser
import tiktoken
from datetime import datetime
from ContextualSearchEngine import ContextualSearchEngine
import time
import threading
from DatabaseHandler import DatabaseHandler
from Modules.RelevanceFilter import RelevanceFilter
from server_config import server_port
import traceback
from constants import USE_GPU_FOR_INFERENCING

# multiprocessing
import multiprocessing
import logging
import logging.handlers

import pandas as pd

# CORS
import aiohttp_cors
from aiohttp import web

dbHandler = DatabaseHandler()
relevanceFilter = RelevanceFilter(databaseHandler=dbHandler)

app = web.Application(client_max_size=1000000 * 32)
# store and retrieve short term memories. Stored as a list of memories.
app['buffer'] = dict()
# store and retrieve long term memories. Implemented as chromadb
app['memory'] = dict()
# store and retrieve notes. Stored as a list of memories.
app['notes'] = dict()

# lower max token decreases latency: https://platform.openai.com/docs/guides/production-best-practices/improving-latencies. On average, each token is 4 characters. We speak 150 wpm, average english word is 4.7 characters
# max_talk_time = 30  # seconds
# max_tokens = (((150 * (max_talk_time / 60)) * 4.7) / 4) * 2  # *2 for response

OPENAI_API_KEY = os.environ['OPENAI_API_KEY']
max_tokens = 1024
app['llm'] = ChatOpenAI(
    model_name="gpt-3.5-turbo-0613",
    temperature=0,
    openai_api_key=OPENAI_API_KEY,
    max_tokens=max_tokens,
)

mostRecentIntermediateTranscript = dict()
intermediateMaxRate = .2 # Only take intermediates every 200ms


async def chat_handler(request):
    startTime = time.time()

    body = await request.json()
    isFinal = body.get('isFinal')
    text = body.get('text')
    timestamp = time.time() # Never use client's timestamp ### body.get('timestamp')
    userId = body.get('userId')

    # 400 if missing params
    if text is None or text == '':
        return web.Response(text='no text in request', status=400)
    if timestamp is None or timestamp == '':
        return web.Response(text='no timestamp in request', status=400)
    if userId is None or userId == '':
        return web.Response(text='no userId in request', status=400)

    # Save to database
    # & Debounce intermediate transcripts by only
    # accepting them every 200ms max
    if userId not in mostRecentIntermediateTranscript:
        mostRecentIntermediateTranscript[userId] = 0

    if isFinal or (timestamp - mostRecentIntermediateTranscript[userId] > intermediateMaxRate):
        #print('\n=== CHAT_HANDLER ===\n{}: {}, {}, {}'.format(
        #    "FINAL" if isFinal else "INTERMEDIATE", text, timestamp, userId))
        if isFinal:
            print('\n=== CHAT_HANDLER ===\n{}: {}, {}, {}'.format("FINAL", text, timestamp, userId))
        if not isFinal:
            mostRecentIntermediateTranscript[userId] = timestamp
        startSaveDbTime = time.time()
        dbHandler.saveTranscriptForUser(
            userId=userId, text=text, timestamp=timestamp, isFinal=isFinal)
        endSaveDbTime = time.time()
        #print("=== CHAT_HANDLER's save DB done in {} SECONDS ===".format(
        #    round(endSaveDbTime - startSaveDbTime, 2)))
    else:
        #print("DEBOUNCING TRANSCRIPT")
        response = ''

    endTime = time.time()
    #print("=== CHAT_HANDLER COMPLETED IN {} SECONDS ===".format(
    #    round(endTime - startTime, 2)))
    return web.Response(text=json.dumps({'success': True, 'message': "Got that chat, yo"}), status=200)


async def button_handler(request):
    body = await request.json()
    button_num = body.get('button_num')
    button_activity = body.get('button_activity')
    timestamp = body.get('timestamp')
    userId = body.get('userId')
    print('\n=== New Request ===\n', button_num,
          button_activity, timestamp, userId)

    # 400 if missing params
    if button_num is None or button_num == '':
        return web.Response(text='no button_num in request', status=400)
    if button_activity is None or button_activity == '':
        return web.Response(text='no button_activity in request', status=400)
    if timestamp is None or timestamp == '':
        return web.Response(text='no timestamp in request', status=400)
    if userId is None or userId == '':
        return web.Response(text='no userId in request', status=400)

    if button_activity:  # True if push down, false if button release
        # save event
        with open(f'./logs/{userId}_events.log', 'a+') as f:
            f.write(str({'text': "BUTTON_DOWN", 'timestamp': timestamp}) + '\n')

        return web.Response(text=json.dumps({'message': "button up activity detected"}), status=200)
    else:
        return web.Response(text=json.dumps({'message': "button up activity detected"}), status=200)


# run tools for subscribed users in background every n ms if there is fresh data to run on
def processing_loop(log_queue):
    print("START PROCESSING LOOP")
    lock = threading.Lock()

    #first, setup logging as this loop is a subprocess
    worker_logger = multiprocessing.get_logger()
    worker_logger.setLevel(logging.INFO)
    handler = logging.handlers.QueueHandler(log_queue)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    worker_logger.addHandler(handler)
    
    #then run the main loop
    while True:
        if not dbHandler.ready:
            print("dbHandler not ready")
            time.sleep(0.1)
            continue
        lock.acquire()

        try:
            pLoopStartTime = time.time()
            # Check for new transcripts
            newTranscripts = dbHandler.getRecentTranscriptsForAllUsers(
                combineTranscripts=True, deleteAfter=True)
            for transcript in newTranscripts:
                print("Run CSE with... userId: '{}' ... text: '{}'".format(
                    transcript['userId'], transcript['text']))
                cseStartTime = time.time()
                cseResponses = cse.contextual_search_engine(
                    transcript['userId'], transcript['text'])
                cseEndTime = time.time()
                print("=== CSE completed in {} seconds ===".format(
                    round(cseEndTime - cseStartTime, 2)))

                #filter responses with relevance filter, then save CSE results to the database
                cseResponsesFiltered = list()
                if cseResponses != None:
                    for res in cseResponses:
                        if res != {} and res != None:
                            if relevanceFilter.shouldRunForText(transcript['userId'], res['name']):
                                cseResponsesFiltered.append(res)
                    dbHandler.addCseResultsForUser(
                        transcript['userId'], cseResponsesFiltered)
        except Exception as e:
            cseResponses = None
            print("Exception in CSE...:")
            print(e)
            traceback.print_exc()
        finally:
            lock.release()
            pLoopEndTime = time.time()
            print("=== processing_loop completed in {} seconds overall ===".format(
                round(pLoopEndTime - pLoopStartTime, 2)))
        time.sleep(2.5)


cse = ContextualSearchEngine(
    relevanceFilter=relevanceFilter, databaseHandler=dbHandler)


async def ui_poll(request, minutes=0.5):
    # parse request
    body = await request.json()
    userId = body.get('userId')
    deviceId = body.get('deviceId')
    features = body.get('features')

    # 400 if missing params
    if userId is None or userId == '':
        return web.Response(text='no userId in request', status=400)
    if deviceId is None or deviceId == '':
        return web.Response(text='no deviceId in request', status=400)
    if features is None or features == '':
        return web.Response(text='no features in request', status=400)
    if "contextual_search_engine" not in features:
        return web.Response(text='contextual_search_engine not in features', status=400)

    resp = dict()

    # get CSE results
    cseResults = dbHandler.getCseResultsForUserDevice(
        userId=userId, deviceId=deviceId)

    if cseResults:
        print("server.py =================================CSERESULT")
        print(cseResults)

    # if cseResults != None and cseResults != []:
    #    print("\n=== CONTEXTUAL_SEARCH_ENGINE ===\n{}".format(cseResults))

    # send response
    if (cseResults) != []:
        resp["success"] = True
        resp["result"] = cseResults
    else:
        resp["success"] = False
    return web.Response(text=json.dumps(resp), status=200)


async def return_image(request):
    requestedImg = request.rel_url.query['img']
    print("Got image request for image: " + requestedImg)
    imgPath = Path(cse.imagePath).joinpath(requestedImg)
    try:
        data = imgPath.read_bytes()
    except:
        print("Error reading requested image: " + requestedImg)
        data = Path('images/404-2.jpg').read_bytes()
    return Response(body=data, content_type="image/jpg")


async def upload_user_data(request):
    post_data = await request.post()

    user_file = post_data.get('custom-file')
    user_id = post_data.get('userId')

    if user_file and user_id:
        # Check if the file is a CSV file by looking at its content type
        if user_file.content_type != 'text/csv':
            return web.Response(text="Uploaded file is not a CSV", status=400)

        # Validate data
        try:
            df = pd.read_csv(user_file.file)
        except Exception:
            return web.Response(text="Bad data format", status=400)

        if not cse.is_custom_data_valid(df):
            return web.Response(text="Bad data format", status=400)

        cse.upload_custom_user_data(user_id, df)

        return web.Response(text="Data processed successfully", status=200)
    else:
        return web.Response(text="Missing user file or user ID in the received data", status=400)

app.add_routes(
    [
        web.post('/chat', chat_handler),
        web.post('/button_event', button_handler),
        web.post('/ui_poll', ui_poll),
        web.post('/upload_userdata', upload_user_data),
        web.get('/image', return_image),
    ]
)

def worker_function(log_queue):
    # Configure the logger within the worker function
    worker_logger = multiprocessing.get_logger()
    worker_logger.setLevel(logging.INFO)
    handler = logging.handlers.QueueHandler(log_queue)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    worker_logger.addHandler(handler)

    # This will log a message to the queue
    worker_logger.info("This is a log message from a child process")
    
    # This will print a message to the queue
    print("This is a print statement from a child process")

# setup and run web app
# CORS allow from all sources
cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(
        allow_credentials=True,
        expose_headers="*",
        allow_headers="*"
    )
})
for route in list(app.router.routes()):
    cors.add(route)

def start_server():
    web.run_app(app, port=server_port)

if __name__ == '__main__':
    #start proccessing loop subprocess to process data as it comes in
    if USE_GPU_FOR_INFERENCING:
        multiprocessing.set_start_method('spawn')
    log_queue = multiprocessing.Queue()
    background_process = multiprocessing.Process(target=processing_loop, args=(log_queue,))
    background_process.start()

    #start web server subprocess
    server_process = multiprocessing.Process(target=start_server)
    server_process.start()

    # Retrieve and process logs and print statements from the queue
    while not log_queue.empty():
        record = log_queue.get()
        # Process log records in the main process
        logger = logging.getLogger(record.name)
        logger.handle(record)

    server_process.join()
    background_process.join()
