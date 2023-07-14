import asyncio
import logging
import os
import socket
import signal
from typing import List

import aiomqtt
from asyncio import CancelledError
from geckolib import GeckoAsyncSpaMan, GeckoSpaEvent, GeckoPump, GeckoSwitch, GeckoSpaState  # type: ignore

# Replace with your own UUID, see https://www.uuidgenerator.net/>
CLIENT_ID = os.environ.get('CLIENT_ID', "d56f0723-59e8-4e01-ad57-65fcf103072f")

host = os.environ.get('SPA_HOST', "inTouch2")
try:
    # Try resolving the input string as a hostname
    SPA_ADDRESS = socket.gethostbyname(host)
except socket.gaierror:
    # Try parsing the input string as an IP address
    socket.inet_aton(host)
    SPA_ADDRESS = host


class MqttSpaManBridge(GeckoAsyncSpaMan):
    """Mqtt spa man implementation"""

    async def handle_event(self, event: GeckoSpaEvent, **kwargs) -> None:
        # Uncomment this line to see events generated
        logger = logging.getLogger()
        logger.debug(f"{event}: {kwargs}")
        pass


async def main() -> None:
    async with MqttSpaManBridge(CLIENT_ID, spa_address=SPA_ADDRESS) as spaman:

        logger = logging.getLogger()

        loop = asyncio.get_event_loop()

        # Wait for descriptors to be available
        await spaman.wait_for_descriptors()

        if len(spaman.spa_descriptors) == 0:
            logger.error("**** There were no spas found on your network.")
            return

        spa_descriptor = spaman.spa_descriptors[0]
        logger.info(f"Connecting to {spa_descriptor.name} at {spa_descriptor.ipaddress} ...")
        await spaman.async_set_spa_info(
            spa_descriptor.ipaddress,
            spa_descriptor.identifier_as_string,
            spa_descriptor.name,
        )
        logger.info("Connected.")

        broker_address = os.environ.get("MQTT_HOST", "mqtt")
        broker_port = int(os.environ.get("MQTT_PORT", "1883"))

        client = aiomqtt.Client(
            hostname=broker_address,
            port=broker_port,
            username=None,
            password=None,
            logger=None,
            client_id=None
        )

        logger.info("Connecting to Spa Manager facade.")
        # Wait for the facade to be ready
        await spaman.wait_for_facade()
        logger.info("Connected.")

        logger.info(f"Connecting to MQTT broker {broker_address}:{broker_port} ...")
        await client.connect()
        logger.info("Connected.")

        set_actions = {}

        mqtt_prefix = "intouch2/" + spaman.facade.name + "/"

        async def listen():
            async with client.messages() as messages:
                await client.subscribe(mqtt_prefix + "#")
                async for message in messages:
                    topic_value = message.topic.value
                    logger.debug("Received mqtt message for topic " + topic_value)
                    action = set_actions.get(topic_value)
                    if action is not None:
                        param = message.payload.decode()
                        logger.info("Executing action for " + topic_value + " -> " + param)
                        await action(param)

        asyncio.ensure_future(listen())

        async def publish_async(src, new_value) -> None:
            logger.info("Publishing " + str(src) + " -> " + str(new_value))
            # Perform asynchronous operations
            await client.publish(mqtt_prefix + str(src), new_value, 0, True)

        def publish(src, new_value) -> None:
            loop.create_task(publish_async(src, new_value))

        async def register_switch(name, switch: GeckoSwitch) -> None:
            await publish_async(name, switch.is_on)

            async def switch_action(value) -> None:
                logger.debug("Switch action " + name + " " + value)
                try:
                    if value == "True":
                        await switch.async_turn_on()
                    elif value == "False":
                        await switch.async_turn_off()
                except CancelledError:
                    logger.exception("Cancelled action " + name)

            action_channel = mqtt_prefix + name + "/set"
            set_actions[action_channel] = switch_action
            logger.debug("Registering action for " + action_channel)
            switch.watch(lambda src, old, new_value: publish(name, switch.is_on))

        async def register_pump(name, pump: GeckoPump) -> None:
            await publish_async(name, pump.mode)

            async def pump_action(value) -> None:
                logger.debug("Pump action " + name + " " + value)
                await pump.async_set_mode(value)

            action_channel = mqtt_prefix + name + "/set"
            set_actions[action_channel] = pump_action
            logger.debug("Registering action for " + action_channel)
            pump.watch(lambda src, old, new_value: publish(name, pump.mode))

        async def set_temp(value):
            await spaman.facade.water_heater.async_set_target_temperature(value)

        set_actions[mqtt_prefix + 'water_heater/target_temperature/set'] = set_temp

        async def publish_heater() -> None:
            # noinspection PyBroadException
            try:
                await publish_async('water_heater/current_temperature', spaman.facade.water_heater.current_temperature)
                await publish_async('water_heater/target_temperature', spaman.facade.water_heater.target_temperature)
                await publish_async('water_heater/real_target_temperature',
                                    spaman.facade.water_heater.real_target_temperature)
                await publish_async('water_heater/temperature_unit', spaman.facade.water_heater.temperature_unit)
                await publish_async('water_heater/current_operation', spaman.facade.water_heater.current_operation)
            except Exception:
                logger.exception("During publish")
                return

        async def set_water_care(value):
            await spaman.facade.water_care.async_set_mode(value)

        set_actions[mqtt_prefix + 'water_care/mode/set'] = set_water_care

        async def publish_water_care():
            mode = spaman.facade.water_care.mode
            logger.info("Publishing water care mode " + str(mode))
            if mode is not None:
                await publish_async('water_care/mode', spaman.facade.water_care.modes[mode])

        async def register_array(name, stuff: List[GeckoSwitch]):
            for index, value in enumerate(stuff):
                await register_switch(name + "/" + str(index), value)

        async def register_pump_array(name, stuff: List[GeckoPump]):
            for index, value in enumerate(stuff):
                await register_pump(name + "/" + str(index), value)

        await register_array('lights', spaman.facade.lights)
        await register_pump_array('pumps', spaman.facade.pumps)
        await register_array('blowers', spaman.facade.blowers)
        if spaman.facade.eco_mode is not None:
            await register_switch('eco_mode', spaman.facade.eco_mode)

        await publish_water_care()
        await publish_heater()

        spaman.facade.water_heater.watch(lambda src, old, _: loop.create_task(publish_heater()))
        spaman.facade.water_care.watch(lambda src, old, _: loop.create_task(publish_water_care()))

        def stop_program():
            logger.warning("Stopping...")
            loop.stop()
            loop.close()

        logger.info("Waiting for events...")

        for signame in ('SIGINT', 'SIGTERM'):
            loop.add_signal_handler(getattr(signal, signame), stop_program)

        while spaman.spa_state.value <= GeckoSpaState.CONNECTED.value:
            await asyncio.sleep(5)

        logger.warning("Exiting. Spa state " + str(spaman.spa_state))
        stop_program()




if __name__ == "__main__":
    # Install logging
    stream_logger = logging.StreamHandler()
    stream_logger.setLevel(logging.DEBUG)
    stream_logger.setFormatter(
        logging.Formatter("%(asctime)s> %(levelname)s %(message)s")
    )
    logging.getLogger().addHandler(stream_logger)

    logging.getLogger().setLevel(os.environ.get('VERBOSITY', logging.WARN))

    asyncio.run(main())
