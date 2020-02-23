from PIL import Image
from picamera.array import PiRGBArray
from picamera import PiCamera
from botocore.exceptions import ClientError
from AWSIoTPythonSDK.MQTTLib import AWSIoTMQTTClient
from time import sleep, time
import sys
from uuid import uuid4
import os
import RPi.GPIO as GPIO
import json
import boto3
import io


################## GENERAL ##################

#SUPPORTED_BINS = ['trash', 'plastic', 'paper', 'metal', 'glass']
SUPPORTED_BINS = ['trash', 'paper']

#GPIO Mode (BOARD / BCM)
GPIO.setmode(GPIO.BCM)

bins = {'trash': {'ultrasound_pins': (24,23), 'servo_pin': 19},
		'paper': {'ultrasound_pins': (21,20), 'servo_pin': 26},
		'plastic': {'ultrasound_pins': (0,0), 'servo_pin': 0},
		'metal': {'ultrasound_pins': (0,0), 'servo_pin': 0},
		'glass': {'ultrasound_pins': (0,0), 'servo_pin': 0},
		'cardboard': {'ultrasound_pins': (0,0), 'servo_pin': 0},
		}
for bin_type in bins.copy():
	if bin_type not in SUPPORTED_BINS:
		del bins[bin_type]

bin_id_file = 'bin_id.txt'
bin_height = 20 #estimate bin height is 20cm


################## Button ##################
BIN_BUTTON_PIN = 27
GPIO.setup(BIN_BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)


################## Servo ##################
DEGREES_0 = 2.5
DEGREES_90 = 7.5
DEGREES_180 = 12.5

for bin_type, bin in bins.items():
	servo_pin = bin['servo_pin']
	GPIO.setup(servo_pin, GPIO.OUT)


################## ULTRASOUND ##################

def ultrasound_distance(GPIO_TRIGGER, GPIO_ECHO):
	#set GPIO direction (IN / OUT)
	GPIO.setup(GPIO_TRIGGER, GPIO.OUT)
	GPIO.setup(GPIO_ECHO, GPIO.IN)

	# set Trigger to HIGH
	GPIO.output(GPIO_TRIGGER, True)

	# set Trigger after 0.01ms to LOW
	sleep(0.00001)
	GPIO.output(GPIO_TRIGGER, False)

	StartTime = time()
	StopTime = time()

	# save StartTime
	while GPIO.input(GPIO_ECHO) == 0:
	    StartTime = time()

	# save time of arrival
	while GPIO.input(GPIO_ECHO) == 1:
	    StopTime = time()

	# time difference between start and arrival
	TimeElapsed = StopTime - StartTime
	# multiply with the sonic speed (34300 cm/s)
	# and divide by 2, because there and back
	distance = (TimeElapsed * 34300) / 2
	return distance


################## REKOGNITION ##################

def start_model(project_arn, model_arn, version_name, min_inference_units):
    client=boto3.client('rekognition')

    try:
        # Start the model
        print('Starting model: ' + model_arn)
        response=client.start_project_version(ProjectVersionArn=model_arn,MinInferenceUnits=min_inference_units)
        # Wait for the model to be in the running state
        project_version_running_waiter = client.get_waiter('project_version_running')
        project_version_running_waiter.wait(ProjectArn=project_arn,VersionNames=[version_name])
        #Get the running status
        describe_response=client.describe_project_versions(ProjectArn=project_arn,VersionNames=[version_name])
        for model in describe_response['ProjectVersionDescriptions']:
            print("Status: " + model['Status'])
            print("Message: " + model['StatusMessage'])
    except Exception as e:
        print(e)

def show_custom_labels(model,bucket,photo, min_confidence):

	client=boto3.client('rekognition')
	# Load image from S3 bucket
	s3_connection = boto3.resource('s3')
	s3_object = s3_connection.Object(bucket,photo)
	s3_response = s3_object.get()
	stream = io.BytesIO(s3_response['Body'].read())
	image=Image.open(stream)

	#Call DetectCustomLabels
	response = client.detect_custom_labels(Image={'S3Object': {'Bucket': bucket,'Name': photo}},MinConfidence=min_confidence,ProjectVersionArn=model)

	highest_detected_label = None
	highest_detected_confidence = 0

	print('Detecting labels...')
	for customLabel in response['CustomLabels']:
		print('Label ' + str(customLabel['Name']))
		print('Confidence ' + str(customLabel['Confidence']))

		if customLabel['Confidence'] > highest_detected_confidence:
			highest_detected_label = customLabel['Name'].lower()
			highest_detected_confidence = customLabel['Confidence']
	print('Done detection')
	return highest_detected_label


################## S3 ##################

def upload_file(file_name, bucket, object_name=None):
    """Upload a file to an S3 bucket

    :param file_name: File to upload
    :param bucket: Bucket to upload to
    :param object_name: S3 object name. If not specified then file_name is used
    :return: True if file was uploaded, else False
    """

    # If S3 object_name was not specified, use file_name
    if object_name is None:
        object_name = file_name

    # Upload the file
    s3_client = boto3.client('s3')
    try:
        response = s3_client.upload_file(file_name, bucket, object_name)
        print("Successfully Uploaded!")
    except ClientError as e:
        return False
    return True


################## MAIN ##################

# Custom MQTT message callback
def customCallback(client, userdata, message):
	action = message.payload.decode()
	if action == 'open':
		print('Opening all bins...')
		for trash_type, bin in bins.items():
			servo = GPIO.PWM(bin['servo_pin'], 50)
			servo.start(7.5)
			sleep(0.1)
			servo.ChangeDutyCycle(DEGREES_180) #open bin
			sleep(1)
			servo.stop()

	if action == 'close':
		print('Opening all bins...')
		for trash_type, bin in bins.items():
			servo = GPIO.PWM(bin['servo_pin'], 50)
			servo.start(7.5)
			sleep(0.1)
			servo.ChangeDutyCycle(DEGREES_0) #close bin
			sleep(1)
			servo.stop()

#check if bin_id exists
if os.path.isfile(bin_id_file):
	with open(bin_id_file, 'r') as f:
		bin_id = f.read()
#if doesnt exist
else:
	bin_id = 'smartbin-{}'.format(uuid4())

host="****************.us-east-1.amazonaws.com"
rootCAPath = os.path.join("certs", "rootca.pem")
certificatePath = os.path.join("certs", "certificate.pem.crt")
privateKeyPath = os.path.join("certs", "private.pem.key")

smartbin = AWSIoTMQTTClient(bin_id)
smartbin.configureEndpoint(host, 8883)
smartbin.configureCredentials(rootCAPath, privateKeyPath, certificatePath)

smartbin.configureOfflinePublishQueueing(-1)  # Infinite offline Publish queueing
smartbin.configureDrainingFrequency(2)  # Draining: 2 Hz
smartbin.configureConnectDisconnectTimeout(10)  # 10 sec
smartbin.configureMQTTOperationTimeout(5)  # 5 sec

# Connect and subscribe to AWS IoT
smartbin.connect()

if not os.path.isfile(bin_id_file):
	smartbin.publish("bin/{}/add".format(bin_id), '{{"bin_id": "{}" }}'.format(bin_id), 1)
	print('Published newly generated bin endpoint client ID: {}'.format(bin_id))
	with open(bin_id_file, 'w') as f:
		f.write(bin_id)

smartbin.subscribe("bin/{}/action".format(bin_id), 1, customCallback)

while True:

	#If button is pushed take picture, analyze using rekognition and open the corresponding bin hole
	if GPIO.input(BIN_BUTTON_PIN) == GPIO.HIGH:
		print("Button was pushed!")
		sleep(2)

		# Take image from picamera and write to file
		filename = str(uuid4())+".jpg"
		write_image_file = open(filename, 'wb')
		camera = PiCamera()
		camera.resolution = (1024, 768)
		camera.start_preview()
		sleep(2)
		camera.capture(write_image_file)
		write_image_file.close()
		camera.close()
		print('Picture saved')

		# Uploads image file to specified s3 bucket
		bucket = "mysmartbin-image-bin"
		upload_file(filename, bucket, object_name=None)

		# Start rekognition model if is is not
		project_arn='arn:aws:rekognition:us-east-1:****************'
		model_arn='arn:aws:rekognition:us-east-1:****************'
		min_inference_units=1
		version_name='MySmartBin-Custom-Label-Training.2020-02-22T01.18.22'
		start_model(project_arn, model_arn, version_name, min_inference_units)

		# Analyse image based on the model above
		min_confidence = 50
		trash_type_detected = show_custom_labels(model_arn,bucket, filename, min_confidence)

		os.remove(filename)

		if trash_type_detected is None:
			trash_type_detected = 'trash'

		if trash_type_detected in SUPPORTED_BINS:
			print('SUPPORTED TRASH TYPE!')
			bin = bins[trash_type_detected]

			servo = GPIO.PWM(bin['servo_pin'], 50)
			servo.start(7.5)
			sleep(0.1)
			print('Opening bin...')
			servo.ChangeDutyCycle(DEGREES_180) #open bin
			sleep(5) #open for x number of seconds
			print('Closing bin...')
			servo.ChangeDutyCycle(DEGREES_0) #close bin
			sleep(2)
			servo.stop()

			ultrasound_pins = bin['ultrasound_pins']
			ultrasound_value = ultrasound_distance(ultrasound_pins[0], ultrasound_pins[1]) #gets ultrasonic sensor value
			percentage = round(((bin_height - ultrasound_value)/bin_height)*100, 2)

			mqtt_message = '{{"bin_id": "{}", "trash_type": "{}", "percentage": {} }}'.format(bin_id, trash_type_detected, percentage)
			print(mqtt_message)
			smartbin.publish("bin/{}/fullness".format(bin_id), mqtt_message, 1)
