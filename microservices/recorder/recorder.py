import sys

sys.path.append("../..")

import asyncio
import logging
import os
import subprocess
from distutils.dir_util import copy_tree
from glob import glob
from shutil import rmtree

import aiormq
import aiormq.types
import config
from craft_vdm import TICK_PADDING, craft_vdm
from ipc import CSGO, RecordingError, SandboxedCSGO, random_string
from resource_semaphore import ResourcePool, ResourceRequest
from sandboxie import Sandboxie

from shared.log import logging_config
from shared.message import MessageError, MessageWrapper

logging_config(config.DEBUG)
log = logging.getLogger(__name__)


async def on_message(message: aiormq.channel.DeliveredMessage):
    wrap = MessageWrapper(
        message=message,
        default_error="An error occurred while recording.",
        ack_on_failure=False,
        raise_on_message_error=True,
        requeue_on_nack=True,
    )

    async with ResourceRequest(pool) as csgo, wrap as ctx:
        csgo: CSGO

        data = ctx.data

        job_id = data["job_id"]
        matchid = data["matchid"]
        demo = rf"{config.DEMO_FOLDER}\{matchid}.dem"
        start = data["start_tick"]
        end = data["end_tick"]
        xuid = data["xuid"]
        output = rf"{config.VIDEO_DIR}\{job_id}.mp4"
        capture_dir = config.TEMP_FOLDER
        fps = data["fps"]
        resolution = data["resolution"]
        video_bitrate = data["video_bitrate"]
        audio_bitrate = data["audio_bitrate"]
        skips = data["skips"]
        video_filters = config.VIDEO_FILTERS if data["color_correction"] else None

        if not os.path.isfile(demo):
            raise ValueError(f"Demo {demo} does not exist.")

        if end < start:
            raise ValueError("FY FAEN RUNAR DU E IDIOT")

        log.info(
            f"Recording player {xuid} from tick {start} to {end} with skips {skips}"
        )

        unblock_string = random_string()

        vdm_script = craft_vdm(
            start_tick=start,
            end_tick=end,
            skips=skips,
            xuid=xuid,
            fps=fps,
            bitrate=video_bitrate,
            capture_dir=capture_dir,
            video_filters=video_filters,
            unblock_string=unblock_string,
        )

        # change res
        await csgo.set_resolution(*resolution)

        # make sure deathmsg doesn't fill up and clear lock spec
        await csgo.run(f"mirv_deathmsg lifetime 0")

        try:
            take_folder = await csgo.playdemo(
                demo=demo,
                vdm=vdm_script,
                unblock_string=unblock_string,
                start_at=start - TICK_PADDING,
            )
        except RecordingError as exc:
            raise MessageError(exc.args[0])

        # mux audio
        wav = glob(take_folder + r"\*.wav")[0]
        subprocess.run(
            [
                config.FFMPEG_BIN,
                "-i",
                take_folder + r"\normal\video.mp4",
                "-i",
                wav,
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                f"{audio_bitrate}k",
                "-y",
                output,
            ],
            capture_output=True,
        )

        rmtree(take_folder)

        await ctx.success()


async def make_sandboxed_csgo(sb: Sandboxie, box: str, sleep) -> CSGO:
    await sb.cleanup(box)

    if sleep is not None:
        await asyncio.sleep(sleep)

    sb.run(
        config.STEAM_BIN,
        # '-nocache',
        "-nofriendsui",
        "-silent",
        "-offline",
        # '-login',
        # config.STEAM_USER,
        # config.STEAM_PASS,
        box=box,
    )

    await asyncio.sleep(32.0)

    port = new_port()

    sb.run(
        config.HLAE_EXE,
        "-csgoLauncher",
        "-noGui",
        "-autoStart",
        "-csgoExe",
        config.CSGO_BIN,
        "-gfxEnabled",
        "true",
        "-gfxWidth",
        str(1280),
        "-gfxHeight",
        str(854),
        "-gfxFull",
        "false",
        "-mmcfgEnabled",
        "true",
        "-mmcfg",
        config.MMCFG_FOLDER,
        "-customLaunchOptions",
        f"-netconport {port} -console -novid",
        box=box,
    )

    return SandboxedCSGO(host="localhost", port=port, box=box)


def make_csgo(port=config.PORT_START):
    subprocess.run(
        [
            config.HLAE_EXE,
            "-csgoLauncher",
            "-noGui",
            "-autoStart",
            "-csgoExe",
            config.CSGO_BIN,
            "-gfxEnabled",
            "true",
            "-gfxWidth",
            str(1280),
            "-gfxHeight",
            str(854),
            "-gfxFull",
            "false",
            "-mmcfgEnabled",
            "true",
            "-mmcfg",
            config.MMCFG_FOLDER,
            "-customLaunchOptions",
            f"-netconport {port} -console -novid",
        ]
    )

    return CSGO("localhost", port=port)


async def on_csgo_error(pool: ResourcePool, csgo: CSGO, exc: Exception):
    if not isinstance(csgo, SandboxedCSGO):
        log.error(
            "Recovering CSGO instances is only supported for sandboxed CSGO instances."
        )
        return

    box_name = csgo.box

    # cleanup the box
    await sb.cleanup(box_name)

    new_csgo = await make_sandboxed_csgo(sb, box=box_name, sleep=None)
    await new_csgo.connect()

    pool.add(new_csgo)


sb = Sandboxie(config.START_BIN)
current_port = config.PORT_START
pool = ResourcePool(on_removal=on_csgo_error)


def new_port():
    global current_port

    current_port += 1
    return current_port


async def main():
    global csgo
    global current_port
    global sb

    logging.getLogger("aiormq").setLevel(logging.INFO)

    # copy over csgo config files
    copy_tree("cfg", config.CSGO_FOLDER + "/cfg")

    if config.SANDBOXED:
        setups = []
        for idx, box_name in enumerate(config.BOXES):
            setups.append(make_sandboxed_csgo(sb, box=box_name, sleep=idx * 5))
            current_port += 1

        csgos = await asyncio.gather(*setups)
    else:
        csgos = [make_csgo()]

    await asyncio.gather(*[csgo.connect() for csgo in csgos])

    csgo = csgos[0]

    startup_commands = ('mirv_block_commands add 5 "\*"', "exec stream")

    for command in startup_commands:
        await asyncio.gather(*[csgo.run(command) for csgo in csgos])

    for csgo in csgos:
        pool.add(csgo)

    mq = await aiormq.connect(config.RABBITMQ_HOST)
    chan = await mq.channel()

    await chan.basic_qos(prefetch_count=len(config.BOXES) if config.SANDBOXED else 1)

    await chan.queue_declare(config.RECORDER_QUEUE)
    await chan.basic_consume(
        queue=config.RECORDER_QUEUE, consumer_callback=on_message, no_ack=False
    )

    log.info("Ready to record!")


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
    loop.run_forever()