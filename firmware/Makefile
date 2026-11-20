# P65 firmware - plain GNU Makefile, no CMake and no external Pico SDK.
# The parts of the SDK that are needed live in sdk/ (see sdk/README.md).
#
#   make                  -> out/p65.uf2 (and out/p65.elf, out/p65.bin)
#   make IRQ_DEBUG=1      VSYNC / IRQ / LCD counters on the USB serial once a second
#   make SHOW_FPS=1       FPS badge on the LCD
#   make flash            copy out/p65.uf2 to a Pico in BOOTSEL mode (Windows only for now)
#   make rom              regenerate include/rom.h from rom/*.a65 (needs ca65 / ld65)
#   make pio              regenerate src/65c02_clk.pio.h (needs pioasm)
#   make clean
#
# Needs: the Arm GNU toolchain (arm-none-eabi-gcc / g++, 10 or newer), GNU make and
# Python 3 (checksum of the boot loader, UF2 file, small file helpers).

CROSS   ?= arm-none-eabi-
CC      := $(CROSS)gcc
CXX     := $(CROSS)g++
OBJCOPY := $(CROSS)objcopy
SIZE    := $(CROSS)size
PYTHON  ?= python
PIOASM  ?= pioasm
OUT     ?= out

IRQ_DEBUG ?= 0
SHOW_FPS  ?= 0

include sdk/sdk.mk

# ---- firmware sources ----
FW_SRCS := \
    src/main.c src/65c02.c src/mapper.c src/tms9918.c src/video.c src/ili9341.c \
    src/keyboard.c src/pcal6524.c src/fs.c src/storage_core.c src/storage_fw.c src/usb_proto.c

# ---- flags ----
ARCH    := -mcpu=cortex-m0plus -mthumb
SDK_DEFS := \
    -DCFG_TUSB_MCU=OPT_MCU_RP2040 -DCFG_TUSB_OS=OPT_OS_PICO \
    -DLIB_PICO_BIT_OPS=1 -DLIB_PICO_BIT_OPS_PICO=1 -DLIB_PICO_DIVIDER=1 -DLIB_PICO_DIVIDER_HARDWARE=1 \
    -DLIB_PICO_DOUBLE=1 -DLIB_PICO_DOUBLE_PICO=1 -DLIB_PICO_FIX_RP2040_USB_DEVICE_ENUMERATION=1 \
    -DLIB_PICO_FLASH=1 -DLIB_PICO_FLOAT=1 -DLIB_PICO_FLOAT_PICO=1 -DLIB_PICO_INT64_OPS=1 \
    -DLIB_PICO_INT64_OPS_PICO=1 -DLIB_PICO_MALLOC=1 -DLIB_PICO_MEM_OPS=1 -DLIB_PICO_MEM_OPS_PICO=1 \
    -DLIB_PICO_MULTICORE=1 -DLIB_PICO_PLATFORM=1 -DLIB_PICO_PRINTF=1 -DLIB_PICO_PRINTF_PICO=1 \
    -DLIB_PICO_RUNTIME=1 -DLIB_PICO_STANDARD_LINK=1 -DLIB_PICO_STDIO=1 -DLIB_PICO_STDIO_USB=1 \
    -DLIB_PICO_STDLIB=1 -DLIB_PICO_SYNC=1 -DLIB_PICO_SYNC_CRITICAL_SECTION=1 -DLIB_PICO_SYNC_MUTEX=1 \
    -DLIB_PICO_SYNC_SEM=1 -DLIB_PICO_TIME=1 -DLIB_PICO_UNIQUE_ID=1 -DLIB_PICO_UTIL=1 \
    -DPICO_BOARD=\"pico\" -DPICO_BUILD=1 -DPICO_CMAKE_BUILD_TYPE=\"Release\" -DPICO_COPY_TO_RAM=0 \
    -DPICO_CXX_ENABLE_EXCEPTIONS=0 -DPICO_NO_FLASH=0 -DPICO_NO_HARDWARE=0 -DPICO_ON_DEVICE=1 \
    -DPICO_RP2040_USB_DEVICE_UFRAME_FIX=1 -DPICO_TARGET_NAME=\"p65\" -DPICO_USE_BLOCKED_RAM=0 -DNDEBUG

P65_DEFS :=
ifeq ($(IRQ_DEBUG),1)
P65_DEFS += -DVIDEO_IRQ_DEBUG=1
endif
ifeq ($(SHOW_FPS),1)
P65_DEFS += -DVIDEO_SHOW_FPS=1
endif

# every sdk/src/<group>/<module>/include directory, in one go
SDK_INCS := $(wildcard sdk/src/*/*/include) \
            sdk/src/boards/include \
            $(wildcard sdk/src/rp2_common/pico_fix/*/include) \
            sdk/generated/pico_base \
            sdk/lib/tinyusb/src sdk/lib/tinyusb/src/common
INCS := -Iinclude $(addprefix -I,$(SDK_INCS))

COMMON   := $(ARCH) -O3 -ffunction-sections -fdata-sections
CFLAGS   := $(COMMON) $(SDK_DEFS) $(P65_DEFS) $(INCS)
CXXFLAGS := $(CFLAGS) -fno-exceptions -fno-unwind-tables -fno-rtti -fno-use-cxa-atexit
LDFLAGS  := $(ARCH) -O3 -DNDEBUG -Wl,--build-id=none --specs=nosys.specs \
            $(foreach w,$(SDK_WRAPS),-Wl,--wrap=$(w)) \
            -Wl,-Map=$(OUT)/p65.map -Wl,--script=sdk/src/rp2_common/pico_standard_link/memmap_default.ld \
            -Wl,-z,max-page-size=4096 -Wl,--gc-sections -Wl,--no-warn-rwx-segments

# ---- helpers that work the same under sh and cmd.exe ----
MKDIR = $(PYTHON) -c "import os,sys;os.makedirs(sys.argv[1],exist_ok=True)"

# ---- objects ----
SRCS := $(FW_SRCS) $(SDK_SRCS)
OBJS := $(addprefix $(OUT)/obj/,$(addsuffix .o,$(SRCS)))
BOOT2_OBJ := $(OUT)/obj/boot2_padded.o

.PHONY: all clean flash rom pio
all: $(OUT)/p65.uf2
	@$(SIZE) $(OUT)/p65.elf

# the firmware's own files are built with warnings on; the SDK's are taken as they are
$(addprefix $(OUT)/obj/,$(addsuffix .o,$(FW_SRCS))): WARN := -Wall -Wextra

$(OUT)/obj/%.c.o: %.c
	@$(MKDIR) $(@D)
	$(CC) $(CFLAGS) $(WARN) -MMD -MP -c $< -o $@

$(OUT)/obj/%.S.o: %.S
	@$(MKDIR) $(@D)
	$(CC) $(CFLAGS) $(WARN) -MMD -MP -c $< -o $@

$(OUT)/obj/%.cpp.o: %.cpp
	@$(MKDIR) $(@D)
	$(CXX) $(CXXFLAGS) $(WARN) -MMD -MP -c $< -o $@

# ---- second stage boot loader (256 bytes in the first flash sector, with checksum) ----
BOOT2_SRC := sdk/src/rp2_common/boot_stage2/compile_time_choice.S
BOOT2_INC := -Isdk/src/rp2_common/boot_stage2/asminclude -Isdk/src/rp2040/hardware_regs/include \
             -Isdk/src/rp2_common/hardware_base/include -Isdk/src/common/pico_base/include \
             -Isdk/generated/pico_base -Isdk/src/boards/include \
             -Isdk/src/rp2_common/pico_platform/include -Isdk/src/rp2_common/boot_stage2/include \
             -Isdk/src/rp2_common/cmsis/include

$(OUT)/boot2/boot2.o: $(BOOT2_SRC)
	@$(MKDIR) $(@D)
	$(CC) -DPICO_BOARD=\"pico\" -DPICO_BUILD=1 -DPICO_NO_HARDWARE=0 -DPICO_ON_DEVICE=1 $(BOOT2_INC) \
	    $(ARCH) -O3 -DNDEBUG -c $< -o $@

$(OUT)/boot2/boot2.elf: $(OUT)/boot2/boot2.o
	$(CC) $(ARCH) -O3 -DNDEBUG -Wl,--build-id=none --specs=nosys.specs -nostartfiles \
	    -Wl,--script=sdk/src/rp2_common/boot_stage2/boot_stage2.ld $< -o $@

$(OUT)/boot2/boot2.bin: $(OUT)/boot2/boot2.elf
	$(OBJCOPY) -Obinary $< $@

$(OUT)/boot2/boot2_padded.S: $(OUT)/boot2/boot2.bin
	$(PYTHON) sdk/src/rp2_common/boot_stage2/pad_checksum -s 0xffffffff $< $@

$(BOOT2_OBJ): $(OUT)/boot2/boot2_padded.S
	@$(MKDIR) $(@D)
	$(CC) $(ARCH) -O3 -DNDEBUG -c $< -o $@

# ---- link ----
$(OUT)/p65.elf: $(OBJS) $(BOOT2_OBJ)
	$(CXX) $(LDFLAGS) $(OBJS) $(BOOT2_OBJ) -o $@

$(OUT)/p65.bin: $(OUT)/p65.elf
	$(OBJCOPY) -Obinary $< $@

$(OUT)/p65.uf2: $(OUT)/p65.bin
	$(PYTHON) tools/bin2uf2.py $< $@

# ---- extras ----
flash: $(OUT)/p65.uf2
	@$(PYTHON) tools/flash.py $<

rom:
	$(PYTHON) tools/make_rom.py

pio:
	$(PIOASM) -o c-sdk src/65c02_clk.pio src/65c02_clk.pio.h

clean:
	@$(PYTHON) -c "import shutil;shutil.rmtree('$(OUT)',ignore_errors=True)"

-include $(OBJS:.o=.d)
