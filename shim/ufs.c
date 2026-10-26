/*
 * Milestone 5 probe: can we talk to the internal UFS that ABL booted from?
 *
 * Runs with the MMU off (all memory Device, so DMA is coherent with no cache
 * maintenance). Dumps the UFSHCI registers ABL left behind and, if the host
 * is enabled and the link is up, points the transfer request list at our own
 * buffers and issues READ-ONLY SCSI commands on LUN 0. Nothing is ever
 * written to the device: there is no SCSI WRITE in this file.
 *
 * Register layout and descriptor formats: JEDEC UFSHCI (JESD223).
 */
#include <stdint.h>
#include "console.h"
#include "ufs.h"

#define UFS_BASE	0x01d84000UL	/* ufshc@1d84000, sweet live FDT */

#define REG_CAP		0x00
#define REG_VER		0x08
#define REG_IS		0x20
#define REG_HCS		0x30
#define REG_HCE		0x34
#define REG_UTRLBA	0x50
#define REG_UTRLBAU	0x54
#define REG_UTRLDBR	0x58
#define REG_UTRLCLR	0x5c
#define REG_UTRLRSR	0x60

#define HCS_DP		(1u << 0)
#define HCS_UTRLRDY	(1u << 1)
#define HCS_UCRDY	(1u << 3)

#define BLOCK		4096u

static inline uint32_t ufs_rd(uint32_t off) { return *(volatile uint32_t *)(UFS_BASE + off); }
static inline void ufs_wr(uint32_t off, uint32_t v) { *(volatile uint32_t *)(UFS_BASE + off) = v; }

/* UTP transfer request descriptor (32 bytes). */
struct utrd {
	uint32_t dw0;		/* [31:28] CT, [26:25] DD, [24] I */
	uint32_t dw1;
	uint32_t dw2;		/* [7:0] OCS */
	uint32_t dw3;
	uint32_t ucdba;		/* command descriptor base, 128-byte aligned */
	uint32_t ucdbau;
	uint32_t resp;		/* [31:16] response UPIU offset, [15:0] length (DWORDs) */
	uint32_t prdt;		/* [31:16] PRDT offset, [15:0] entries (DWORDs/entries) */
};

/* UTP command descriptor: command UPIU, response UPIU, PRDT. */
struct ucd {
	uint8_t cmd[512];
	uint8_t resp[512];
	struct {
		uint32_t addr_lo, addr_hi, rsvd, dbc;	/* dbc = byte count - 1 */
	} prdt[1];
};

static struct utrd utrl[32] __attribute__((aligned(1024)));
static struct ucd ucd __attribute__((aligned(128)));
static uint8_t data[2 * BLOCK] __attribute__((aligned(4096)));

static uint64_t ticks(void)
{
	uint64_t t;
	__asm__ volatile("isb; mrs %0, cntvct_el0" : "=r"(t));
	return t;
}

static void zero(void *p, unsigned n)
{
	volatile uint8_t *b = p;
	for (unsigned i = 0; i < n; i++)
		b[i] = 0;
}

static uint32_t be32(const uint8_t *p)
{
	return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

/*
 * Issue one SCSI command (read direction or no data) in slot 0.
 * Returns 0 on success, otherwise a code describing the failure stage.
 */
static uint32_t scsi(uint8_t lun, const uint8_t *cdb, unsigned cdb_len, void *buf, uint32_t len,
    uint32_t *ocs_out, uint32_t *status_out)
{
	static uint8_t tag;
	zero(&utrl[0], sizeof(utrl[0]));
	zero(&ucd, sizeof(ucd));

	/* Command UPIU: header (12 bytes), expected length, CDB. */
	ucd.cmd[0] = 0x01;			/* transaction type: COMMAND */
	ucd.cmd[1] = len ? 0x40 : 0x00;		/* flags: R (data in) */
	ucd.cmd[2] = lun;
	ucd.cmd[3] = ++tag;
	ucd.cmd[12] = len >> 24;
	ucd.cmd[13] = len >> 16;
	ucd.cmd[14] = len >> 8;
	ucd.cmd[15] = len;
	for (unsigned i = 0; i < cdb_len; i++)
		ucd.cmd[16 + i] = cdb[i];

	uintptr_t ucd_pa = (uintptr_t)&ucd;
	utrl[0].dw0 = (1u << 28) | ((len ? 2u : 0u) << 25) | (1u << 24);	/* UFS storage, dev->host */
	utrl[0].dw2 = 0x0f;			/* OCS: invalid until the host updates it */
	utrl[0].ucdba = (uint32_t)ucd_pa;
	utrl[0].ucdbau = (uint32_t)(ucd_pa >> 32);
	utrl[0].resp = (512u / 4) << 16 | (512u / 4);
	if (len) {
		uintptr_t buf_pa = (uintptr_t)buf;
		ucd.prdt[0].addr_lo = (uint32_t)buf_pa;
		ucd.prdt[0].addr_hi = (uint32_t)(buf_pa >> 32);
		ucd.prdt[0].dbc = len - 1;
		utrl[0].prdt = (1024u / 4) << 16 | 1;
	}
	__asm__ volatile("dsb sy" ::: "memory");

	ufs_wr(REG_UTRLDBR, 1);
	uint64_t start = ticks();
	while (ufs_rd(REG_UTRLDBR) & 1) {
		if (ticks() - start > 19200000) {	/* 1 s */
			ufs_wr(REG_UTRLCLR, ~1u);	/* clear slot 0 (write 0 to the bit) */
			return 1;			/* timeout */
		}
	}
	__asm__ volatile("dsb sy" ::: "memory");
	*ocs_out = utrl[0].dw2 & 0xff;
	*status_out = ucd.resp[7];		/* SCSI status in the response UPIU */
	if (*ocs_out)
		return 2;
	if (ucd.resp[0] != 0x21)		/* RESPONSE UPIU */
		return 3;
	return *status_out ? 4 : 0;
}

static uint32_t read_blocks(uint32_t lba, uint32_t count, void *buf)
{
	uint8_t cdb[10] = { 0x28, 0, lba >> 24, lba >> 16, lba >> 8, lba, 0, count >> 8, count, 0 };
	uint32_t ocs, st;
	return scsi(0, cdb, sizeof(cdb), buf, count * BLOCK, &ocs, &st);
}

static uint64_t le64(const uint8_t *p)
{
	uint64_t v = 0;
	for (int i = 7; i >= 0; i--)
		v = v << 8 | p[i];
	return v;
}

/* Name the filesystem whose superblock starts at block lba (f2fs/ext4). */
static const char *fs_type(uint32_t lba)
{
	if (read_blocks(lba, 1, data))
		return "read error";
	const uint8_t *sb = data + 1024;
	if (sb[0] == 0x10 && sb[1] == 0x20 && sb[2] == 0xf5 && sb[3] == 0xf2)
		return "f2fs";
	if (sb[56] == 0x53 && sb[57] == 0xef)
		return "ext4";
	return "other";
}

/* One line per GPT entry of at least 64 MiB: name, first LBA, size in MiB (4K blocks). */
static void list_partitions(uint32_t entry_lba, uint32_t nent)
{
	static uint8_t ents[128 * 128] __attribute__((aligned(4096)));
	uint32_t blocks = (nent * 128 + BLOCK - 1) / BLOCK;
	if (read_blocks(entry_lba, blocks, ents)) {
		con_str("UFS PART", "read error");
		return;
	}
	for (uint32_t i = 0; i < nent; i++) {
		const uint8_t *e = ents + i * 128;
		uint64_t first = le64(e + 32), last = le64(e + 40);
		if (!first)
			continue;
		char name[17];
		for (int c = 0; c < 16; c++) {
			uint8_t ch = e[56 + 2 * c];
			name[c] = (ch >= 32 && ch < 127) ? ch : (ch ? '?' : 0);
		}
		name[16] = 0;
		if (((last - first + 1) >> 8) < 64)	/* keep the screen to the big ones */
			continue;
		con_hex2(name, "lba=", (uint32_t)first, "MiB=", (uint32_t)((last - first + 1) >> 8));
		if (!__builtin_strcmp(name, "userdata") || !__builtin_strcmp(name, "cust") ||
		    !__builtin_strcmp(name, "cache"))
			con_str(" fs", fs_type((uint32_t)first));
	}
}

void test_ufs(void)
{
	con_str("UFS", "milestone 5 probe (read-only)");
	uint32_t cap = ufs_rd(REG_CAP), hcs = ufs_rd(REG_HCS), hce = ufs_rd(REG_HCE);
	con_hex2("UFS", "ver=", ufs_rd(REG_VER), "cap=", cap);
	con_hex2("UFS", "hce=", hce, "hcs=", hcs);
	con_hex2("UFS", "is=", ufs_rd(REG_IS), "rsr=", ufs_rd(REG_UTRLRSR));
	con_hex2("UFS", "utrlba=", ufs_rd(REG_UTRLBA), "hi=", ufs_rd(REG_UTRLBAU));
	con_hex2("UFS", "dbr=", ufs_rd(REG_UTRLDBR), "nutrs=", (cap & 0x1f) + 1);

	if (!(hce & 1) || !(hcs & HCS_DP) || !(hcs & HCS_UTRLRDY)) {
		con_str("UFS", "host not enabled/ready: stop");
		return;
	}
	if (ufs_rd(REG_UTRLDBR)) {
		con_str("UFS", "requests outstanding: stop");
		return;
	}

	/* Take over the transfer request list: stop, point at ours, run. */
	ufs_wr(REG_UTRLRSR, 0);
	uintptr_t list = (uintptr_t)utrl;
	ufs_wr(REG_UTRLBA, (uint32_t)list);
	ufs_wr(REG_UTRLBAU, (uint32_t)(list >> 32));
	ufs_wr(REG_UTRLRSR, 1);
	con_hex2("UFS", "ours=", (uint32_t)list, "rsr=", ufs_rd(REG_UTRLRSR));
	/*
	 * Never ring a doorbell unless the host really uses our list: otherwise
	 * it would execute whatever ABL left in its own slot 0 (maybe a write).
	 */
	if (ufs_rd(REG_UTRLBA) != (uint32_t)list || ufs_rd(REG_UTRLBAU) != (uint32_t)(list >> 32) ||
	    !(ufs_rd(REG_UTRLRSR) & 1)) {
		con_str("UFS", "list base not taken: stop");
		return;
	}

	uint32_t ocs = 0, st = 0, rc;
	static const uint8_t tur[6] = { 0x00 };
	/* A first command may report a unit attention; retry a few times. */
	for (int i = 0; i < 3; i++) {
		rc = scsi(0, tur, sizeof(tur), 0, 0, &ocs, &st);
		con_hex2("UFS TUR", "rc=", rc, "ocs/st=", ocs << 8 | st);
		if (!rc)
			break;
	}

	static const uint8_t cap10[10] = { 0x25 };	/* READ CAPACITY(10) */
	rc = scsi(0, cap10, sizeof(cap10), data, 8, &ocs, &st);
	con_hex2("UFS CAP", "rc=", rc, "ocs/st=", ocs << 8 | st);
	if (!rc)
		con_hex2("UFS LUN0", "lastlba=", be32(data), "blk=", be32(data + 4));

	/* READ(10) LBA 0, 2 blocks: protective MBR and the GPT header. */
	static const uint8_t rd10[10] = { 0x28, 0, 0, 0, 0, 0, 0, 0, 2, 0 };
	rc = scsi(0, rd10, sizeof(rd10), data, 2 * BLOCK, &ocs, &st);
	con_hex2("UFS READ", "rc=", rc, "ocs/st=", ocs << 8 | st);
	if (rc)
		return;
	con_hex2("UFS MBR", "sig=", data[510] | data[511] << 8, "type=", data[450]);
	const uint8_t *gpt = data + BLOCK;
	char sig[9];
	for (int i = 0; i < 8; i++)
		sig[i] = (gpt[i] >= 32 && gpt[i] < 127) ? gpt[i] : '.';
	sig[8] = 0;
	con_str("UFS GPT", sig);
	uint32_t nent = gpt[80] | gpt[81] << 8 | gpt[82] << 16, esz = gpt[84] | gpt[85] << 8;
	uint32_t elba = gpt[72] | gpt[73] << 8;
	con_hex2("UFS GPT", "entries=", nent, "lba=", elba);
	if (esz != 128 || nent > 128)
		return;
	list_partitions(elba, nent);
}

/* ---- SWEETLOG pull: read the persistent console log (patch 0047) ----
 *
 * The kernel appends every console byte to a 4 MiB RAM ring and, once the
 * userdata partition is known writable, flushes new blocks to the LAST
 * 1025 blocks of userdata: block 0 = header ("SWEETLOG", u32 version,
 * u32 ring size, u64 total bytes written, u64 flush seq), then the ring.
 * Byte n of the console stream lives at ring offset n % ring size.
 * Here we read that area back and print the tail on the framebuffer.
 */

#define SWEET_LOG_BLOCKS	1025

extern char _end[];  /* logarea is placed after the whole image at runtime */

/* Freestanding: __builtin_memcmp can emit an external memcmp call. */
static int mem_eq(const void *a, const void *b, uint32_t n)
{
	const uint8_t *x = a, *y = b;
	for (uint32_t i = 0; i < n; i++)
		if (x[i] != y[i])
			return 0;
	return 1;
}

static void con_lines(const char *buf, uint32_t n)
{
	static char line[256];
	uint32_t at = 0;
	while (at < n) {
		uint32_t end = at;
		while (end < n && buf[end] != '\n')
			end++;
		uint32_t len = end - at;
		if (len > sizeof(line) - 2)
			len = sizeof(line) - 2;
		for (uint32_t i = 0; i < len; i++) {
			uint8_t c = (uint8_t)buf[at + i];
			line[i] = (c >= 32 && c < 127) ? (char)c : '?';
		}
		line[len] = 0;
		con_str("L", line);
		at = end + 1;
	}
}

/* Dump the last ~24 lines of the LAST printable run in [from, lim):
   the end of that run is where writing stopped = freeze point. */
static void dump_freshest(const uint8_t *buf, uint32_t from, uint32_t lim)
{
	uint32_t lr_start = 0, lr_len = 0, cs = 0, cl = 0;
	for (uint32_t o = from; o <= lim; o++) {
		uint8_t c = o < lim ? buf[o] : 0;
		if (c && ((c >= 32 && c < 127) || c == '\n' || c == '\r' || c == '\t')) {
			if (!cl)
				cs = o;
			cl++;
			continue;
		}
		if (cl) {
			lr_start = cs;
			lr_len = cl;
			cl = 0;
		}
	}
	if (lr_len < 128) {
		con_str("LOGPULL", "no sizable text yet");
		return;
	}
	uint32_t rend = lr_start + lr_len;
	uint32_t from2 = lr_start, lines = 0;
	for (uint32_t o = rend; o-- > lr_start; ) {
		if (buf[o] == '\n' && ++lines > 24) {
			from2 = o + 1;
			break;
		}
	}
	con_lines((const char *)buf + from2, rend - from2);
	con_str("LOGPULL", "end of dump");
}

void ufs_logpull(void)
{
	uint32_t ocs, st;
	uint8_t *logarea = (uint8_t *)((((uintptr_t)_end) + 4095) & ~4095UL);

	/* Host takeover, same safety checks as test_ufs. */
	uint32_t hcs = ufs_rd(REG_HCS), hce = ufs_rd(REG_HCE);
	if (!(hce & 1) || !(hcs & HCS_DP) || !(hcs & HCS_UTRLRDY)) {
		con_str("LOGPULL", "host not enabled/ready");
		return;
	}
	if (ufs_rd(REG_UTRLDBR)) {
		con_str("LOGPULL", "requests outstanding");
		return;
	}
	ufs_wr(REG_UTRLRSR, 0);
	uintptr_t list = (uintptr_t)utrl;
	ufs_wr(REG_UTRLBA, (uint32_t)list);
	ufs_wr(REG_UTRLBAU, (uint32_t)(list >> 32));
	ufs_wr(REG_UTRLRSR, 1);
	if (ufs_rd(REG_UTRLBA) != (uint32_t)list || ufs_rd(REG_UTRLBAU) != (uint32_t)(list >> 32) ||
	    !(ufs_rd(REG_UTRLRSR) & 1)) {
		con_str("LOGPULL", "list base not taken");
		return;
	}
	static const uint8_t tur[6] = { 0 };
	for (int i = 0; i < 3; i++)
		if (!scsi(0, tur, sizeof(tur), 0, 0, &ocs, &st))
			break;

	/* Find userdata in the GPT. */
	static const uint8_t rd10[10] = { 0x28, 0, 0, 0, 0, 0, 0, 0, 2, 0 };
	if (scsi(0, rd10, sizeof(rd10), data, 2 * BLOCK, &ocs, &st)) {
		con_str("LOGPULL", "read MBR/GPT failed");
		return;
	}
	const uint8_t *gpt = data + BLOCK;
	if (gpt[72] == 0 && gpt[73] == 0 && gpt[74] == 0 && gpt[75] == 0) {
		con_str("LOGPULL", "no GPT");
		return;
	}
	uint32_t nent = gpt[80] | gpt[81] << 8 | gpt[82] << 16;
	uint32_t elba = gpt[72] | gpt[73] << 8;
	static uint8_t ents[128 * 128] __attribute__((aligned(4096)));
	uint32_t eb = (nent * 128 + BLOCK - 1) / BLOCK;
	if (read_blocks(elba, eb, ents)) {
		con_str("LOGPULL", "read GPT entries failed");
		return;
	}
	uint64_t first = 0, nblk = 0;
	for (uint32_t i = 0; i < nent; i++) {
		const uint8_t *e = ents + i * 128;
		uint64_t f = le64(e + 32), l = le64(e + 40);
		if (!f)
			continue;
		char name[17];
		for (int c = 0; c < 16; c++) {
			uint8_t ch = e[56 + 2 * c];
			name[c] = (ch >= 32 && ch < 127) ? ch : (ch ? '?' : 0);
		}
		name[16] = 0;
		if (!__builtin_strcmp(name, "userdata")) {
			first = f;
			nblk = l - f + 1;
			break;
		}
	}
	if (!first || nblk < SWEET_LOG_BLOCKS) {
		con_str("LOGPULL", "userdata not found/too small");
		return;
	}
	uint64_t base = first + nblk - SWEET_LOG_BLOCKS;
	con_hex2("LOGPULL", "area lba=", (uint32_t)base, "blocks=", SWEET_LOG_BLOCKS);

	/* Read header + ring, 256 KiB per SCSI command (larger single
	   transfers fail on this controller). */
	for (uint32_t off = 0; off < SWEET_LOG_BLOCKS; off += 64) {
		uint32_t n = SWEET_LOG_BLOCKS - off;
		if (n > 64)
			n = 64;
		if (read_blocks((uint32_t)(base + off), n, logarea + off * BLOCK)) {
			con_str("LOGPULL", "area read failed");
			return;
		}
	}

	/* 4 MiB scratch right past the image: safe in LOGPULL mode, which
	   never boots xnu (and thus never touches the ramdisk region). */
	uint32_t lim = SWEET_LOG_BLOCKS * BLOCK;
	static const char magic[8] = "SWEETLOG";
	uint32_t magic_at = 0xffffffff;

	if (mem_eq(logarea, magic, 8)) {
		magic_at = 0;
	} else {
		/* Forensics: the header block was never written (or was
		   written elsewhere).  Scan the whole area for the magic
		   and for kernel log signatures, then dump the freshest
		   text: the flush writes ring data FIRST and the header
		   LAST, so an interrupted flush leaves log text with no
		   header.  The end of the last printable run is exactly
		   where the kernel stopped writing = freeze point. */
		static const char sig1[8] = "BSD root";
		static const char sig2[9] = "SWEET UFS";
		uint32_t log_at = 0xffffffff;
		for (uint32_t o = 0; o + 9 < lim; o++) {
			uint8_t c = logarea[o];
			if (magic_at == 0xffffffff && c == 'S' &&
			    mem_eq(logarea + o, magic, 8))
				magic_at = o;
			if (log_at == 0xffffffff && (c == 'B' || c == 'S') &&
			    (mem_eq(logarea + o, sig1, 8) ||
			     mem_eq(logarea + o, sig2, 9)))
				log_at = o;
		}
		if (magic_at != 0xffffffff) {
			con_hex2("LOGPULL", "magic at +", magic_at, 0, 0);
		} else {
			con_str("LOGPULL", "no magic anywhere");
		}
		if (log_at == 0xffffffff) {
			con_str("LOGPULL", "no log text (never flushed)");
			return;
		}
		con_hex2("LOGPULL", "log at +", log_at, 0, 0);
		dump_freshest(logarea, log_at, lim);
		if (magic_at == 0xffffffff)
			return;
	}

	const uint8_t *hdr = logarea + magic_at;
	uint32_t rsize = hdr[8] | hdr[9] << 8 | hdr[10] << 16 | hdr[11] << 24;
	uint64_t total = le64(hdr + 16);
	con_hex2("SWEETLOG", "ring=", rsize, "total lo=", (uint32_t)total);
	if (rsize != 4 * 1024 * 1024) {
		con_str("SWEETLOG", "unexpected ring size");
		return;
	}
	if (total == 0) {
		/* Header marker flushed but no data committed: dump
		   whatever text got written past the header. */
		con_str("SWEETLOG", "total=0, dumping partial data");
		dump_freshest(logarea, magic_at + BLOCK, lim);
		return;
	}

	/* Linearize the last ~4 KiB of the console stream. */
	const uint8_t *ring = hdr + BLOCK;
	uint64_t tailn = total < 4000 ? total : 4000;
	uint64_t start = total - tailn;
	static char tail[4096];
	for (uint64_t i = 0; i < tailn; i++)
		tail[i] = (char)ring[(start + i) % rsize];
	con_lines(tail, (uint32_t)tailn);
	con_str("LOGPULL", "end of console log");
}
