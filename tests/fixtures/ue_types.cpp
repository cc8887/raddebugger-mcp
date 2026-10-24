// Independent MSVC/PDB layout fixture. This does not link or run Unreal Engine.
// Field order models the selected UE4.26 Win64 editor container/text layouts;
// PDB types, rather than fixed byte offsets, identify container element strides.
#include <windows.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cwchar>

using int32 = std::int32_t;
using uint8 = std::uint8_t;
using uint16 = std::uint16_t;
using uint32 = std::uint32_t;

template<int IndexBits> struct TSizedDefaultAllocator {
    template<class T> struct ForElementType { T* Data; };
};
template<class T, class Allocator = TSizedDefaultAllocator<32>> struct TArray {
    typename Allocator::template ForElementType<T> AllocatorInstance;
    int32 ArrayNum;
    int32 ArrayMax;
};
struct FString { TArray<TCHAR> Data; };

static FString string_buffer(TCHAR* data, int32 units) {
    FString result = {};
    result.Data.AllocatorInstance.Data = data;
    result.Data.ArrayNum = result.Data.ArrayMax = units;
    return result;
}
template<std::size_t N> static FString literal(const TCHAR (&data)[N]) {
    return string_buffer(const_cast<TCHAR*>(data), static_cast<int32>(N));
}

FString g_chinese = literal(L"限帧");
FString g_mixed = literal(L"FPS 帧率 / test");
FString g_emoji = literal(L"播放 \U0001F3AE\U0001F680");
FString g_empty = {};
TCHAR g_embedded_buffer[] = {L'A', 0, L'中', 0};
FString g_embedded_nul = string_buffer(g_embedded_buffer, 4);
TCHAR g_nonterminated_buffer[] = {L'无', L'结', L'尾'};
FString g_no_nul = string_buffer(g_nonterminated_buffer, 3);
TCHAR g_invalid_utf16_buffer[] = {static_cast<TCHAR>(0xd800), 0};
FString g_invalid_utf16 = string_buffer(g_invalid_utf16_buffer, 2);
TCHAR g_long_buffer[601] = {};
FString g_long = string_buffer(g_long_buffer, 601);

// The shared-reference wrapper has the same two-pointer shape as UE4 Win64.
struct FSharedReferencer { void* ReferenceController; };
template<class T> struct TSharedPtr { T* Object; FSharedReferencer SharedReferenceCount; };
template<class T> struct TSharedRef { T* Object; FSharedReferencer SharedReferenceCount; };
class FTextHistory {
public:
    virtual ~FTextHistory() = default;
    uint16 Revision = 0;
};
class FTextHistory_Base : public FTextHistory {
public:
    FString SourceString = {};
};
class ITextData {
public:
    virtual ~ITextData() = default;
    virtual const FString& GetDisplayString() const = 0;
};
template<class HistoryType> class TTextData : public ITextData {
public:
    TSharedPtr<FString> LocalizedString = {};
    HistoryType History;
};
template<class HistoryType> class TLocalizedTextData : public TTextData<HistoryType> {
public:
    const FString& GetDisplayString() const override { return *this->LocalizedString.Object; }
};
struct FCriticalSection { CRITICAL_SECTION CriticalSection; };
template<class HistoryType> class TGeneratedTextData : public TTextData<HistoryType> {
public:
    FString DisplayString = {};
    FCriticalSection PersistTextCS = {};
    const FString& GetDisplayString() const override { return DisplayString; }
};
class FUnknownTextData : public ITextData {
public:
    std::uint64_t UnrecognizedPayload = 0x1122334455667788;
    const FString& GetDisplayString() const override { return g_empty; }
};
struct FText { TSharedRef<ITextData> TextData; uint32 Flags; };
TLocalizedTextData<FTextHistory_Base> g_localized_data;
TGeneratedTextData<FTextHistory_Base> g_generated_data;
FUnknownTextData g_unknown_data;
FText g_localized_text = {{&g_localized_data, {}}, 0};
FText g_generated_text = {{&g_generated_data, {}}, 0};
FText g_unknown_text = {{&g_unknown_data, {}}, 0};

struct FNameEntryId { uint32 Value; };
struct FName { FNameEntryId ComparisonIndex; FNameEntryId DisplayIndex; uint32 Number; };
FName g_case_preserving_name = {};
struct FNameEntryHeader { uint16 bIsWide : 1; uint16 Len : 15; };
struct FNameEntry {
    FNameEntryId ComparisonId;
    FNameEntryHeader Header;
    union { char AnsiName[1024]; TCHAR WideName[1024]; };
};
struct FNameDebugVisualizer {
    static constexpr uint32 OffsetBits = 16;
    static constexpr uint32 EntryStride = alignof(FNameEntry);
    static constexpr uint32 OffsetMask = 65535;
};
alignas(4) uint8 g_name_block0[262144] = {};
alignas(4) uint8 g_name_block1[262144] = {};
uint8* GNameBlocksDebug[2] = {g_name_block0, g_name_block1};

static FName ansi_name(uint32 id, const char* text, uint32 number = 0) {
    auto* entry = reinterpret_cast<FNameEntry*>(GNameBlocksDebug[id >> 16] + 4 * (id & 65535));
    entry->ComparisonId.Value = id;
    entry->Header.bIsWide = 0;
    entry->Header.Len = static_cast<uint16>(std::strlen(text));
    std::memcpy(entry->AnsiName, text, std::strlen(text));
    return {{id}, {id}, number};
}
static FName wide_name(uint32 id, const TCHAR* text) {
    auto* entry = reinterpret_cast<FNameEntry*>(GNameBlocksDebug[id >> 16] + 4 * (id & 65535));
    entry->ComparisonId.Value = id;
    entry->Header.bIsWide = 1;
    entry->Header.Len = static_cast<uint16>(std::wcslen(text));
    std::memcpy(entry->WideName, text, std::wcslen(text) * sizeof(TCHAR));
    return {{id}, {id}, 0};
}

template<int N> struct TInlineAllocator {
    template<class T> struct ForElementType {
        T InlineData[N];
        typename TSizedDefaultAllocator<32>::template ForElementType<T> SecondaryData;
    };
};
using FDefaultBitArrayAllocator = TInlineAllocator<4>;
template<class Allocator = FDefaultBitArrayAllocator> struct TBitArray {
    typename Allocator::template ForElementType<uint32> AllocatorInstance;
    int32 NumBits;
    int32 MaxBits;
};
template<int Size, int Alignment> struct alignas(Alignment) TAlignedBytes { uint8 Pad[Size]; };
struct FFreeListLink { int32 PrevFreeIndex; int32 NextFreeIndex; };
template<class T> union TSparseArrayElementOrFreeListLink {
    T ElementData;
    // Naming the link avoids MSVC's anonymous-struct extension; storage is
    // unchanged, and the reader addresses the PDB-typed ElementData member.
    FFreeListLink FreeList;
};
struct FSetElementId { int32 Index; };
template<class K, class V> struct TTuple { K Key; V Value; };
template<class T> struct TSetElement { T Value; FSetElementId HashNextId; int32 HashIndex; };
template<class T> struct TSparseArray {
    using Slot = TSparseArrayElementOrFreeListLink<TAlignedBytes<sizeof(T), alignof(T)>>;
    TArray<Slot> Data;
    TBitArray<> AllocationFlags;
    int32 FirstFreeIndex;
    int32 NumFreeIndices;
};
struct FDefaultSetAllocator {};
template<class K, class V, bool AllowDuplicateKeys> struct TDefaultMapHashableKeyFuncs {};
template<class T, class KeyFuncs, class Allocator = FDefaultSetAllocator> struct TSet {
    TSparseArray<TSetElement<T>> Elements;
    TInlineAllocator<1>::ForElementType<FSetElementId> Hash;
    int32 HashSize;
};
template<class K, class V, class Allocator = FDefaultSetAllocator,
         class KeyFuncs = TDefaultMapHashableKeyFuncs<K, V, false>> struct TMap {
    TSet<TTuple<K, V>, KeyFuncs, Allocator> Pairs;
};
struct UFixtureObject { int32 Id; FString Label; };
UFixtureObject g_objects[4] = {{101, literal(L"first")}, {102, literal(L"中文对象")},
                             {103, literal(L"third")}, {104, literal(L"fourth")}};
int32 g_number_storage[] = {7, -2, 0, 12345, 7};
TArray<int32> g_numbers = {{g_number_storage}, 5, 5};
TArray<int32> g_empty_array = {};
using NameMap = TMap<FName, UFixtureObject*>;
using StringMap = TMap<FString, int32>;
using StringStringMap = TMap<FString, FString>;
NameMap g_name_map_inline = {};
NameMap g_name_map_secondary = {};
NameMap g_empty_map = {};
StringMap g_string_map = {};
StringStringMap g_string_string_map = {};
TSparseArray<TSetElement<TTuple<FName, UFixtureObject*>>>::Slot g_inline_slots[4] = {};
TSparseArray<TSetElement<TTuple<FName, UFixtureObject*>>>::Slot g_secondary_slots[132] = {};
TSparseArray<TSetElement<TTuple<FString, int32>>>::Slot g_string_slots[4] = {};
TSparseArray<TSetElement<TTuple<FString, FString>>>::Slot g_string_string_slots[3] = {};
uint32 g_secondary_bitmap[5] = {};

template<class K, class V, std::size_t N>
static void init_map(TMap<K, V>& map, typename TSparseArray<TSetElement<TTuple<K, V>>>::Slot (&slots)[N],
                     uint32* secondary = nullptr) {
    auto& elements = map.Pairs.Elements;
    elements.Data = {{slots}, static_cast<int32>(N), static_cast<int32>(N)};
    elements.AllocationFlags.NumBits = static_cast<int32>(N);
    elements.AllocationFlags.MaxBits = secondary ? static_cast<int32>((N + 31) / 32 * 32) : 128;
    elements.AllocationFlags.AllocatorInstance.SecondaryData.Data = secondary;
    elements.FirstFreeIndex = 0;
    elements.NumFreeIndices = static_cast<int32>(N);
    map.Pairs.Hash.InlineData[0].Index = -1;
    for (std::size_t i = 0; i < N; ++i) {
        slots[i].FreeList.PrevFreeIndex = i ? static_cast<int32>(i - 1) : -1;
        slots[i].FreeList.NextFreeIndex = i + 1 < N ? static_cast<int32>(i + 1) : -1;
    }
}
template<class K, class V> static void put(TMap<K, V>& map, int32 slot, K key, V value) {
    auto& elements = map.Pairs.Elements;
    TSetElement<TTuple<K, V>> element = {{key, value}, map.Pairs.Hash.InlineData[0], 0};
    std::memcpy(&elements.Data.AllocatorInstance.Data[slot].ElementData, &element, sizeof(element));
    auto* bits = elements.AllocationFlags.AllocatorInstance.SecondaryData.Data;
    if (!bits) { bits = elements.AllocationFlags.AllocatorInstance.InlineData; }
    bits[slot / 32] |= 1u << (slot % 32);
    --elements.NumFreeIndices;
    map.Pairs.Hash.InlineData[0].Index = slot;
    map.Pairs.HashSize = 1;
}
template<class K, class V> static void link_free_slots(TMap<K, V>& map) {
    auto& elements = map.Pairs.Elements;
    auto* bits = elements.AllocationFlags.AllocatorInstance.SecondaryData.Data;
    if (!bits) { bits = elements.AllocationFlags.AllocatorInstance.InlineData; }
    int32 previous = -1;
    elements.FirstFreeIndex = -1;
    for (int32 i = 0; i < elements.Data.ArrayNum; ++i) {
        if (bits[i / 32] & (1u << (i % 32))) { continue; }
        auto& slot = elements.Data.AllocatorInstance.Data[i];
        slot.FreeList.PrevFreeIndex = previous;
        slot.FreeList.NextFreeIndex = -1;
        if (previous >= 0) { elements.Data.AllocatorInstance.Data[previous].FreeList.NextFreeIndex = i; }
        else { elements.FirstFreeIndex = i; }
        previous = i;
    }
}

volatile LONG g_type_probe = 0;
__declspec(noinline) void types_ready() {
    ++g_type_probe; // MCP_TYPES_READY: all globals are initialized before this line.
}
static void initialize_types() {
    for (int32 i = 0; i < 600; ++i) { g_long_buffer[i] = L'中'; }
    g_localized_data.LocalizedString.Object = &g_chinese;
    g_generated_data.DisplayString = g_emoji;
    const FName alpha = ansi_name(16, "Alpha");
    const FName gamma = ansi_name(48, "Gamma");
    const FName delta = ansi_name(64, "Delta");
    const FName wide = wide_name(80, L"中名");
    const FName canonical_numbered = ansi_name(112, "player", 2);
    g_case_preserving_name = ansi_name(96, "Player", 2);
    g_case_preserving_name.ComparisonIndex = canonical_numbered.ComparisonIndex;
    auto* display_entry = reinterpret_cast<FNameEntry*>(g_name_block0 + 4 * 96);
    display_entry->ComparisonId = canonical_numbered.ComparisonIndex;
    const FName cross_block = ansi_name(0x10010, "CrossBlock");
    init_map(g_name_map_inline, g_inline_slots);
    put(g_name_map_inline, 0, alpha, &g_objects[0]);
    put(g_name_map_inline, 2, gamma, static_cast<UFixtureObject*>(nullptr));
    put(g_name_map_inline, 3, delta, &g_objects[3]);
    link_free_slots(g_name_map_inline);
    init_map(g_name_map_secondary, g_secondary_slots, g_secondary_bitmap);
    put(g_name_map_secondary, 0, delta, &g_objects[3]);
    put(g_name_map_secondary, 31, wide, &g_objects[1]);
    put(g_name_map_secondary, 128, g_case_preserving_name, &g_objects[2]);
    put(g_name_map_secondary, 131, cross_block, &g_objects[0]);
    link_free_slots(g_name_map_secondary);
    init_map(g_string_map, g_string_slots);
    put(g_string_map, 0, literal(L"alpha"), int32{11});
    put(g_string_map, 2, literal(L"中文"), int32{-7});
    put(g_string_map, 3, literal(L"rocket \U0001F680"), int32{42});
    link_free_slots(g_string_map);
    init_map(g_string_string_map, g_string_string_slots);
    put(g_string_string_map, 0, literal(L"first"), literal(L"中英 mixed"));
    put(g_string_string_map, 2, literal(L"second"), literal(L"\U0001F3AE"));
    link_free_slots(g_string_string_map);
    g_empty_map.Pairs.Elements.FirstFreeIndex = -1;
}

static_assert(sizeof(TCHAR) == 2 && sizeof(void*) == 8, "Windows x64 UTF16 profile required");
static_assert(sizeof(FString) == 16 && sizeof(FText) == 24, "Unexpected text layout");
static_assert(sizeof(FTextHistory_Base) == 32, "Unexpected text history layout");
static_assert(sizeof(FName) == 12 && sizeof(FNameEntryHeader) == 2 && alignof(FNameEntry) == 4,
              "UE4.26 editor case-preserving FName layout required");
static_assert(sizeof(NameMap) == 80, "Unexpected selected UE4.26 map layout");
static_assert(sizeof(g_string_string_slots[0]) != sizeof(g_inline_slots[0]),
              "Regression must cover more than one PDB-derived slot stride");

int main(int argc, char** argv) {
    char token_probe[2];
    if (GetEnvironmentVariableA("RAD_MCP_TOKEN", token_probe, sizeof(token_probe))) { return 98; }
    if (argc > 1 && std::strcmp(argv[1], "--wait-for-debugger") == 0) {
        if (argc != 3 || !argv[2][0]) { return 97; }
        // Attaching can briefly resume the process before the driver's first
        // stopped state. The driver creates this private gate only after the
        // types_ready breakpoint has been installed, so timing cannot skip it.
        for (;;) {
            const DWORD attributes = GetFileAttributesA(argv[2]);
            if (IsDebuggerPresent() && attributes != INVALID_FILE_ATTRIBUTES
                && !(attributes & FILE_ATTRIBUTE_DIRECTORY)) {
                break;
            }
            Sleep(10);
        }
    }
    initialize_types();
    types_ready();
    if (argc > 1 && std::strcmp(argv[1], "--loop") == 0) {
        for (;;) { InterlockedIncrement(&g_type_probe); Sleep(20); }
    }
    std::puts("UE4_LAYOUT_FIXTURE_OK (not Unreal runtime acceptance)");
    return g_type_probe == 1 ? 0 : 1;
}
