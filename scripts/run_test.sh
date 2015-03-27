function get_arguments() {
    export FUEL_MASTER_IP=${1:-172.16.52.108}
    export FUEL_MASTER_PASSWD=${2:-test37}
    export EXTERNAL_IP=${3:-172.16.55.2}
    export KEY_FILE_NAME=${4:-disk_io_perf.pem}
    export FILE_TO_TEST=${5:-file.txt}
    export RESULT_FILE=${6:-results.txt}
    export TIMEOUT=${7:-60}

    if [ $KEY_FILE_NAME does not exist ];
    then
       echo "File $KEY_FILE_NAME does not exist."
    fi

    echo "Fuel master IP: $FUEL_MASTER_IP"
    echo "Fuel master password: $FUEL_MASTER_PASSWD"
    echo "External IP: $EXTERNAL_IP"
    echo "Key file name: $KEY_FILE_NAME"
    echo  "Timeout: $TIMEOUT"
}

# note : function will works properly only when image dame is single string without spaces that can brake awk
function wait_image_active() {
	image_state="none"
	image_name="$IMAGE_NAME"
    counter=0

	while [ ! "$image_state" eq "active" ] ; do
		sleep 1
		image_state=$(glance image-list | grep "$image_name" | awk '{print $12}')
		echo $image_state
		counter=$((counter + 1))

		if [ "$counter" -eq "$TIMEOUT" ]
        then
            echo "Time limit exceed"
            break
        fi
	done
}


function wait_floating_ip() {
	floating_ip="|"
	vm_name=$VM_NAME
    counter=0

	while [ "$floating_ip" != "|" ] ; do
		sleep 1
		floating_ip=$(nova floating-ip-list | grep "$vm_name" | awk '{print $13}' | head -1)
		counter=$((counter + 1))

		if [ $counter -eq $TIMEOUT ]
        then
            echo "Time limit exceed"
            break
        fi
	done
}


function wait_vm_deleted() {
	vm_name=$(nova list| grep "$VM_NAME"| awk '{print $4}'| head -1)
    counter=0

	while [ ! -z $vm_name ] ; do
		sleep 1
		vm_name=$(nova list| grep "$VM_NAME"| awk '{print $4}'| head -1)
		counter=$((counter + 1))

		if [ "$counter" -eq $TIMEOUT ]
        then
            echo "Time limit exceed"
            break
        fi
	done
}


function get_floating_ip() {
    IP=$(nova floating-ip-list | grep "$FLOATING_NET" | awk '{if ($5 == "-") print $2}' | head -n1)

    if [ -z "$IP" ]; then # fix net name
        IP=$(nova floating-ip-create "$FLOATING_NET"| awk '{print $2}')

        if [ -z "$list" ]; then
            echo "Cannot allocate new floating ip"
            exit
        fi
    fi
}

function get_openrc() {
    source run_vm.sh "$FUEL_MASTER_IP" "$FUEL_MASTER_PASSWD" "$EXTERNAL_IP"
    source `get_openrc`

    list=$(nova list)
    if [ "$list" == "" ]; then
        echo "openrc variables are unset or set to the empty string"
    fi

    VM_IP=$IP
    echo "VM IP: $VM_IP"
    echo 'AUTH_URL: "$OS_AUTH_URL"'
}


get_arguments $1 $2 $3 $4 $5 $6 $6 $7
echo "getting openrc from controller node"
get_openrc
echo "openrc has been activated on your machine"
get_floating_ip
echo "floating ip has been found"
bash prepare.sh
echo "Image has been sended to glance"
wait_image_active
echo "Image has been saved"
VOL_ID=$(boot_vm)
echo "VM has been booted"
wait_floating_ip
echo "Floating IP has been obtained"
source `prepare_vm`
echo "VM has been prepared"

# sudo bash ../single_node_test_short.sh $FILE_TO_TEST $RESULT_FILE

# ssh $SSH_OPTS -i $KEY_FILE_NAME ubuntu@$VM_IP \
#     "cd /tmp/io_scenario; echo 'results' > $RESULT_FILE; \
#     curl -X POST -d @$RESULT_FILE http://http://172.16.52.80/api/test --header 'Content-Type:application/json'"

nova delete $VM_NAME
wait_vm_deleted
echo "$VM_NAME has been deleted successfully"
cinder delete $VOL_ID
echo "Volume has been deleted $VOL_ID"
