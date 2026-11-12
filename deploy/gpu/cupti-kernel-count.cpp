// Process-local proof that CUDA executed device kernels during a CFD run.
// Based on the CUDA 12.6 CUPTI cupti_trace_injection sample's injection and
// activity-buffer lifecycle. Do not treat a kernel count as CFD quality proof.
#include <cupti.h>

#include <atomic>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

namespace {

constexpr size_t kActivityBufferBytes = 8 * 1024 * 1024;
constexpr size_t kActivityBufferAlignment = 8;

std::atomic<bool> initialized{false};
std::atomic<bool> failed{false};
std::atomic<uint64_t> device_kernels{0};
std::atomic<uint64_t> dropped_records{0};

void fail(const char* operation, CUptiResult status) {
    failed.store(true, std::memory_order_relaxed);
    std::fprintf(stderr, "WINDCATCHER_GPU_PROBE_ERROR=%s:%d\n",
                 operation, static_cast<int>(status));
    std::fflush(stderr);
}

void CUPTIAPI request_buffer(uint8_t** buffer, size_t* size,
                             size_t* max_records) {
    void* memory = nullptr;
    if (posix_memalign(&memory, kActivityBufferAlignment,
                       kActivityBufferBytes) != 0) {
        failed.store(true, std::memory_order_relaxed);
        std::fprintf(stderr, "WINDCATCHER_GPU_PROBE_ERROR=buffer_allocation\n");
        *buffer = nullptr;
        *size = 0;
        *max_records = 0;
        return;
    }
    *buffer = static_cast<uint8_t*>(memory);
    *size = kActivityBufferBytes;
    *max_records = 0;
}

void CUPTIAPI complete_buffer(CUcontext context, uint32_t stream_id,
                              uint8_t* buffer, size_t, size_t valid_bytes) {
    if (buffer != nullptr && valid_bytes > 0) {
        CUpti_Activity* record = nullptr;
        for (;;) {
            const CUptiResult status =
                cuptiActivityGetNextRecord(buffer, valid_bytes, &record);
            if (status == CUPTI_ERROR_MAX_LIMIT_REACHED) {
                break;
            }
            if (status != CUPTI_SUCCESS) {
                fail("get_next_record", status);
                break;
            }
            if (record->kind == CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL) {
                device_kernels.fetch_add(1, std::memory_order_relaxed);
            }
        }
    }
    if (context != nullptr) {
        size_t dropped = 0;
        const CUptiResult status =
            cuptiActivityGetNumDroppedRecords(context, stream_id, &dropped);
        if (status == CUPTI_SUCCESS) {
            dropped_records.fetch_add(dropped, std::memory_order_relaxed);
        } else {
            fail("get_dropped_records", status);
        }
    }
    std::free(buffer);
}

void finalize() {
    // A forced flush also collects records left in partial activity buffers.
    const CUptiResult status = cuptiActivityFlushAll(1);
    if (status != CUPTI_SUCCESS) {
        fail("flush", status);
    }
    const uint64_t count = device_kernels.load(std::memory_order_relaxed);
    const uint64_t dropped = dropped_records.load(std::memory_order_relaxed);
    if (failed.load(std::memory_order_relaxed) || dropped != 0 || count == 0) {
        std::fprintf(stderr,
                     "WINDCATCHER_GPU_PROBE_UNVERIFIED=kernels:%llu,dropped:%llu\n",
                     static_cast<unsigned long long>(count),
                     static_cast<unsigned long long>(dropped));
        std::fflush(stderr);
        return;
    }
    std::fprintf(stdout, "WINDCATCHER_GPU_DEVICE_KERNELS=%llu\n",
                 static_cast<unsigned long long>(count));
    std::fflush(stdout);
}

}  // namespace

extern "C" int InitializeInjection() {
    if (initialized.exchange(true, std::memory_order_acq_rel)) {
        return 1;
    }
    CUptiResult status =
        cuptiActivityRegisterCallbacks(request_buffer, complete_buffer);
    if (status != CUPTI_SUCCESS) {
        fail("register_callbacks", status);
        return 0;
    }
    status = cuptiActivityEnable(CUPTI_ACTIVITY_KIND_CONCURRENT_KERNEL);
    if (status != CUPTI_SUCCESS) {
        fail("enable_concurrent_kernel", status);
        return 0;
    }
    if (std::atexit(finalize) != 0) {
        failed.store(true, std::memory_order_relaxed);
        std::fprintf(stderr, "WINDCATCHER_GPU_PROBE_ERROR=atexit\n");
        return 0;
    }
    return 1;
}
