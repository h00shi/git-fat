from __future__ import print_function, with_statement,unicode_literals

import sys
import hashlib
import tempfile
import os
import subprocess
import shlex
import shutil
import itertools
import threading
import time
import collections

if sys.version_info[0] > 2:
    unicode = str
else:
    from io import open
    
def touni(s,encoding='utf8'):
    """Automate unicode conversion"""
    if isinstance(s,(str,unicode)):
        return s
    if hasattr(s,'decode'):
        return s.decode(encoding)
    raise ValueError('Cound not decode')
    
def tobytes(s,encoding='utf8'):
    """Automatic byte conversion"""
    if isinstance(s,bytes):
        return s
    if hasattr(s,'encode'):
        return s.encode(encoding)
    raise ValueError('Could not encode')
  
try:
    from subprocess import check_output
    del check_output
except ImportError:
    def backport_check_output(*popenargs, **kwargs):
        r"""Run command with arguments and return its output as a byte string.

        Backported from Python 2.7 as it's implemented as pure python on stdlib.

        >>> check_output(['/usr/bin/python', '--version'])
        Python 2.6.2
        """
        process = subprocess.Popen(stdout=subprocess.PIPE, *popenargs, **kwargs)
        output, unused_err = process.communicate()
        retcode = process.poll()
        if retcode:
            cmd = kwargs.get("args")
            if cmd is None:
                cmd = popenargs[0]
            error = subprocess.CalledProcessError(retcode, cmd)
            error.output = output
            raise error
        return output
    subprocess.check_output = backport_check_output

BLOCK_SIZE = 4096

def verbose_stderr(*args, **kwargs):
    return print(*args, file=sys.stderr, **kwargs)
def verbose_ignore(*args, **kwargs):
    pass

def mkdir_p(path):
    import errno
    try:
        os.makedirs(path)
    except OSError as exc: # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else: raise

def umask():
    """Get umask without changing it."""
    old = os.umask(0)
    os.umask(old)
    return old

def readblocks(stream):
    bytes = 0
    while True:
        data = stream.read(BLOCK_SIZE)
        bytes += len(data)
        if not data:
            break
        yield data
def cat_iter(initer, outstream):
    for block in initer:
        outstream.write(block)
def cat(instream, outstream):
    return cat_iter(readblocks(instream), outstream)
def difftreez_reader(input):
    """Incremental reader for git diff-tree -z output

    :oldmode newmode oldsha1 newsha1 modflag\0filename\0:oldmode newmode ...
    """
    buffer = []
    partial = ''
    while True:
        newread = input.read(BLOCK_SIZE)
        if not newread:
            break
        newread = touni(newread)
        partial += newread
        while True:
            head, sep, partial = partial.partition('\0')
            if not sep:
                partial = head
                break
            buffer.append(head)
            if len(buffer) == 2:
                oldmode, newmode, oldhash, newhash, modflag = buffer[0].split()
                path = buffer[1]
                yield (newhash, modflag, path)
                buffer = []
def gitconfig_get(name, file=None):
    args = ['git', 'config', '--get']
    if file is not None:
        args += ['--file', file]
    args.append(name)
    p = subprocess.Popen(args, stdout=subprocess.PIPE)
    output = p.communicate()[0].strip()
    if p.returncode and file is None:
        return None
    elif p.returncode:
        return gitconfig_get(name)
    else:
        return touni(output)
def gitconfig_set(name, value, file=None):
    args = ['git', 'config']
    if file is not None:
        args += ['--file', file]
    args += [name, value]
    p = subprocess.check_call(args)

class GitFat(object):
    DecodeError = RuntimeError
    def __init__(self):
        self.verbose = verbose_stderr if os.environ.get('GIT_FAT_VERBOSE') else verbose_ignore
        try:
            self.gitroot = subprocess.check_output('git rev-parse --show-toplevel'.split()).strip()
            self.gitroot = touni(self.gitroot)
        except subprocess.CalledProcessError:
            sys.exit(1)
        self.gitdir = subprocess.check_output('git rev-parse --git-dir'.split()).strip()
        self.gitdir = touni(self.gitdir)
        self.objdir = os.path.join(self.gitdir, 'fat', 'objects')
        if os.environ.get('GIT_FAT_VERSION') == '1':
            self.encode = self.encode_v1
        else:
            self.encode = self.encode_v2
        def magiclen(enc):
            return len(enc(hashlib.sha1(b'dummy').hexdigest(), 5))
        self.magiclen = magiclen(self.encode) # Current version
        self.magiclens = [magiclen(enc) for enc in [self.encode_v1, self.encode_v2]] # All prior versions
    def setup(self):
        mkdir_p(self.objdir)
    def is_init_done(self):
        return gitconfig_get('filter.fat.clean') or gitconfig_get('filter.fat.smudge')
    def assert_init_done(self):
        if not self.is_init_done():
            sys.stderr.write('fatal: git-fat is not yet configured in this repository.\n')
            sys.stderr.write('Run "git fat init" to configure.\n')
            sys.exit(1)
    def get_rsync(self):
        cfgpath   = os.path.join(self.gitroot,'.gitfat')
        remote    = gitconfig_get('rsync.remote', file=cfgpath)
        ssh_port  = gitconfig_get('rsync.sshport', file=cfgpath)
        ssh_user  = gitconfig_get('rsync.sshuser', file=cfgpath)
        options   = gitconfig_get('rsync.options', file=cfgpath)
        if remote is None:
            raise RuntimeError('No rsync.remote in %s' % cfgpath)
        return remote, ssh_port, ssh_user, options
    def get_rsync_command(self,push):
        (remote, ssh_port, ssh_user, options) = self.get_rsync()
        if push:
            self.verbose('Pushing to %s' % (remote))
        else:
            self.verbose('Pulling from %s' % (remote))
        cmd = ['rsync', '--progress', '--ignore-existing', '--from0', '--files-from=-']
        rshopts = ''
        if ssh_user:
            rshopts += ' -l ' + ssh_user
        if ssh_port:
            rshopts += ' -p ' + ssh_port
        if rshopts:
            cmd.append('--rsh=ssh' + rshopts)
        if options:
            cmd += options.split(' ')
        if push:
            cmd += [self.objdir + '/', remote + '/']
        else:
            cmd += [remote + '/', self.objdir + '/']
        return cmd
    def revparse(self, revname):
        return touni(subprocess.check_output(['git', 'rev-parse', revname]).strip())
    def encode_v1(self, digest, bytes):
        'Produce legacy representation of file to be stored in repository.'
        return '#$# git-fat %s\n' % (digest,)
    def encode_v2(self, digest, bytes):
        'Produce representation of file to be stored in repository. 20 characters can hold 64-bit integers.'
        return '#$# git-fat %s %20d\n' % (digest, bytes)
    def decode(self, string, noraise=False):
        cookie = '#$# git-fat '
        string = touni(string)
        if string.startswith(cookie):
            parts = string[len(cookie):].split()
            digest = parts[0]
            bytes = int(parts[1]) if len(parts) > 1 else None
            return digest, bytes
        elif noraise:
            return None, None
        else:
            raise GitFat.DecodeError('Could not decode %s' % (string))
    def decode_stream(self, stream):
        'Return digest if git-fat cache, otherwise return iterator over entire file contents'
        preamble = stream.read(self.magiclen)
        try:
            return self.decode(preamble)
        except GitFat.DecodeError:
            # Not sure if this is the right behavior
            return itertools.chain([preamble], readblocks(stream)), None
    def decode_file(self, fname):
        # Fast check
        try:
            stat = os.lstat(fname)
        except OSError:
            return False, None
        if stat.st_size != self.magiclen:
            return False, None
        # read file
        try:
            digest, bytes = self.decode_stream(open(fname,'rb'))
        except IOError:
            return False, None
        if isinstance(digest, str):
            return digest, bytes
        else:
            return None, bytes
    def decode_clean(self, body):
        '''
        Attempt to decode version in working tree. The tree version could be changed to have a more
        useful message than the machine-readable copy that goes into the repository. If the tree
        version decodes successfully, it indicates that the fat data is not currently available in
        this repository.
        '''
        digest, bytes = self.decode(body, noraise=True)
        return digest
    def filter_clean(self, instream, outstreamclean):
        h = hashlib.new('sha1')
        bytes = 0
        fd, tmpname = tempfile.mkstemp(dir=self.objdir)
        try:
            ishanging = False
            cached = False                # changes to True when file is cached
            with os.fdopen(fd, 'wb') as cache:
                outstream = cache
                firstblock = True
                for block in readblocks(instream):
                    if firstblock:
                        if len(block) == self.magiclen and self.decode_clean(block[0:self.magiclen]):
                            ishanging = True              # Working tree version is verbatim from repository (not smudged)
                            outstream = outstreamclean
                        firstblock = False
                    h.update(block)
                    bytes += len(block)
                    outstream.write(block)
                outstream.flush()
            digest = h.hexdigest()
            objfile = os.path.join(self.objdir, digest)
            if not ishanging:
                if os.path.exists(objfile):
                    self.verbose('git-fat filter-clean: cache already exists %s' % objfile)
                    os.remove(tmpname)
                else:
                    # Set permissions for the new file using the current umask
                    os.chmod(tmpname, int('444', 8) & ~umask())
                    os.rename(tmpname, objfile)
                    self.verbose('git-fat filter-clean: caching to %s' % objfile)
                cached = True
                outstreamclean.write(tobytes(self.encode(digest, bytes)))
        finally:
            if not cached:
                os.remove(tmpname)

    def cmd_filter_clean(self):
        '''
        The clean filter runs when a file is added to the index. It gets the "smudged" (tree)
        version of the file on stdin and produces the "clean" (repository) version on stdout.

        Clean filter is the process of telling git what to track (the placeholder file)
        instead of the actual fat content (~ add)
        '''
        self.setup()
        if hasattr(sys.stdin,'buffer'):
            stdin,stdout = sys.stdin.buffer,sys.stdout.buffer
        else:
            stdin,stdout = sys.stdin,sys.stdout
        self.filter_clean(stdin, stdout)

    def cmd_filter_smudge(self):
        """Smudge filter is the process of replacing the placeholder file
        with the actual fat content (~ pull)"""
        self.setup()
        if hasattr(sys.stdin,'buffer'):
            stdin,stdout = sys.stdin.buffer,sys.stdout.buffer
        else:
            stdin,stdout = sys.stdin,sys.stdout
        result, bytes = self.decode_stream(stdin)
        if isinstance(result, str):       # We got a digest
            objfile = os.path.join(self.objdir, result)
            try:
                cat(open(objfile,'rb'), stdout)
                self.verbose('git-fat filter-smudge: restoring from %s' % objfile)
            except IOError:                 # file not found
                self.verbose('git-fat filter-smudge: fat object missing %s' % objfile)
                stdout.write(tobytes(self.encode(result, bytes)))   # could leave a better notice about how to recover this file
        else:                             # We have an iterable over the original input.
            self.verbose('git-fat filter-smudge: not a managed file')
            cat_iter(result, stdout)
    def catalog_objects(self):
        return set(os.listdir(self.objdir))

    def referenced_objects(
        self,
        all=False,
        rev=None,
        with_address=False,
    ):
        referenced = set()
        if all:
            rev = '--all'
        elif rev is None:
            # Shayan: This gets the sha1 of HEAD
            rev = self.revparse('HEAD')
        hash2address = { }
        # Revision list gives us object names to inspect with cat-file...
        # Shayan: returns list of all files that git manages.
        p1 = subprocess.Popen(['git','rev-list','--objects',rev], stdout=subprocess.PIPE)
        # Shayan: Reads from p1 writes to p2. Coool...
        def cut_sha1hash(input, output):
            for line in input:
                line = touni(line)
                line_split = line.split()
                if len(line_split) >= 2:
                    output.write(tobytes(line_split[0] + '\n'))
                    if with_address: hash2address[line_split[0]] = line_split[1]
            output.close()
        # ...`cat-file --batch-check` filters for git-fat object candidates in bulk...
        p2 = subprocess.Popen(['git','cat-file','--batch-check'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        # Shayan: Reads from p2 writes to p3. Coool...
        def filter_gitfat_candidates(input, output):
            for line in input:
                line = touni(line)
                objhash, objtype, size = line.split()
                if objtype == 'blob' and int(size) in self.magiclens:
                    output.write(tobytes(objhash + '\n'))
            output.close()
        # ...`cat-file --batch` provides full contents of git-fat candidates in bulk
        p3 = subprocess.Popen(['git','cat-file','--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        # Stream data: p1 | cut_thread | p2 | filter_thread | p3
        cut_thread = threading.Thread(target=cut_sha1hash, args=(p1.stdout, p2.stdin))
        filter_thread = threading.Thread(target=filter_gitfat_candidates, args=(p2.stdout, p3.stdin))
        cut_thread.start()
        filter_thread.start()
        # Process metadata + content format provided by `cat-file --batch`
        while True:
            metadata_line = p3.stdout.readline()
            # print("META LINE: ", metadata_line)
            if not metadata_line:
                break  # EOF
            objhash, objtype, size_str = touni(metadata_line).split()
            size, bytes_read = int(size_str), 0
            # We know from filter that item is a candidate git-fat object and
            # is small enough to read into memory and process
            content = b''
            while bytes_read < size:
                data = p3.stdout.read(size - bytes_read)
                if not data:
                    break  # EOF
                content += data
                bytes_read += len(data)
            try:
                #print("Content: ", content)
                decoded = self.decode(content)
                #print("Decoded Content: ", decoded)
                fathash = touni(decoded[0])
                if not with_address:
                    referenced.add(fathash)
                else: 
                    referenced.add((fathash,objhash,hash2address[objhash]))
            except GitFat.DecodeError:
                pass
            # Consume LF record delimiter in `cat-file --batch` output
            bytes_read = 0
            while bytes_read < 1:
                data = p3.stdout.read(1)
                if not data:
                    break  # EOF
                bytes_read += len(data)
        # Ensure everything is cleaned up
        cut_thread.join()
        filter_thread.join()
        p1.wait()
        p2.wait()
        p3.wait()
        return referenced
    
    def orphan_files(self, patterns=[]):
        'generator for all orphan placeholders in the working tree'
        if not patterns or patterns == ['']:
            patterns = ['.']
        for fname in subprocess.check_output(['git', 'ls-files', '-z'] + patterns).split(b'\x00')[:-1]:
            fname = touni(fname)
            digest = self.decode_file(fname)[0]
            if digest:
                yield (digest, fname)

    def cmd_status(self, args):
        self.setup()
        catalog = self.catalog_objects()
        refargs = dict()
        if '--all' in args:
            refargs['all'] = True
        referenced = self.referenced_objects(**refargs)
        garbage = catalog - referenced
        orphans = referenced - catalog
        if '--all' in args:
            for obj in referenced:
                print(obj)
        if orphans:
            print('Orphan objects:')
            for orph in orphans:
                print('    ' + orph)
        if garbage:
            print('Garbage objects:')
            for g in garbage:
                print('    ' + g)
    def is_dirty(self):
        return subprocess.call(['git', 'diff-index', '--quiet', 'HEAD']) == 0
    def cmd_push(self, args):
        'Push anything that I have stored and referenced'
        self.setup()
        # Default to push only those objects referenced by current HEAD
        # (includes history). Finer-grained pushing would be useful.
        pushall = '--all' in args
        files = self.referenced_objects(all=pushall) & self.catalog_objects()
        cmd = self.get_rsync_command(push=True)
        self.verbose('Executing: %s' % ' '.join(cmd))
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        p.communicate(input=b'\x00'.join(tobytes(file) for file in files))
        if p.returncode:
            sys.exit(p.returncode)
    def checkout(self, show_orphans=False):
        'Update any stale files in the present working tree'
        self.assert_init_done()
        for digest, fname in self.orphan_files():
            objpath = os.path.join(self.objdir, digest)
            if os.access(objpath, os.R_OK):
                print('Restoring %s -> %s' % (digest, fname))
                # The output of our smudge filter depends on the existence of
                # the file in .git/fat/objects, but git caches the file stat
                # from the previous time the file was smudged, therefore it
                # won't try to re-smudge. I don't know a git command that
                # specifically invalidates that cache, but changing the mtime
                # on the file will invalidate the cache.
                # Here we set the mtime to mtime + 1. This is an improvement
                # over touching the file as it catches the edgecase where a
                # git-checkout happens within the same second as a git fat
                # checkout.
                stat = os.lstat(fname)
                os.utime(fname, (stat.st_atime, stat.st_mtime + 1))
                # This re-smudge is essentially a copy that restores
                # permissions.
                subprocess.check_call(
                    ['git', 'checkout-index', '--index', '--force', fname])
            elif show_orphans:
                print('Data unavailable: %s %s' % (digest,fname))
    def cmd_pull(self, args):
        'Pull anything that I have referenced, but not stored'
        self.setup()
        refargs = dict()
        if '--all' in args:
            refargs['all'] = True
        for arg in args:
            if arg.startswith('-') or len(arg) != 40:
                continue
            rev = self.revparse(arg)
            if rev:
                refargs['rev'] = rev
        files = self.filter_objects(refargs, self.parse_pull_patterns(args))
        cmd = self.get_rsync_command(push=False)
        self.verbose('Executing: %s' % ' '.join(cmd))
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        p.communicate(input=b'\x00'.join(tobytes(file) for file in files))
        if p.returncode:
            sys.exit(p.returncode)
        self.checkout()

    def parse_pull_patterns(self, args):
        if '--' not in args:
            return ['']
        else:
            idx = args.index('--')
            patterns  = args[idx+1:] #we don't care about '--'
            return patterns

    def filter_objects(self, refargs, patterns):
        files = self.referenced_objects(**refargs) - self.catalog_objects()
        if refargs.get('all'): # Currently ignores patterns; can we efficiently do both?
            return files
        orphans_matched = list(self.orphan_files(patterns))
        orphans_objects = set(map(lambda x: x[0], orphans_matched))
        return files & orphans_objects

    def cmd_checkout(self, args):
        self.checkout(show_orphans=True)

    def cmd_gc(self):
        garbage = self.catalog_objects() - self.referenced_objects()
        print('Unreferenced objects to remove: %d' % len(garbage))
        for obj in garbage:
            fname = os.path.join(self.objdir, obj)
            print('%10d %s' % (os.stat(fname).st_size, obj))
            os.remove(fname)

    def cmd_verify(self):
        """Print details of git-fat objects with incorrect data hash"""
        corrupted_objects = []
        for obj in self.catalog_objects():
            fname = os.path.join(self.objdir, obj)
            h = hashlib.new('sha1')
            for block in readblocks(open(fname,'rb')):
                h.update(block)
            data_hash = h.hexdigest()
            if obj != data_hash:
                corrupted_objects.append((obj, data_hash))
        if corrupted_objects:
            print('Corrupted objects: %d' % len(corrupted_objects))
            for obj, data_hash in corrupted_objects:
                print('%s data hash is %s' % (obj, data_hash))
            sys.exit(1)

    def cmd_init(self):
        self.setup()
        if self.is_init_done():
            print('Git fat already configured, check configuration in .git/config')
        else:
            gitconfig_set('filter.fat.clean', 'git-fat filter-clean')
            gitconfig_set('filter.fat.smudge', 'git-fat filter-smudge')
            print('Initialized git fat')
    def gen_large_blobs(self, revs, threshsize):
        """Build dict of all blobs"""
        time0 = time.time()
        def hash_only(input, output):
            """The output of git rev-list --objects shows extra info for blobs, subdirectory trees, and tags.
            This truncates to one hash per line.
            """
            for line in input:
                output.write(line[:40] + b'\n')
            output.close()
        revlist = subprocess.Popen(['git', 'rev-list', '--all', '--objects'], stdout=subprocess.PIPE, bufsize=-1)
        objcheck = subprocess.Popen(['git', 'cat-file', '--batch-check'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=-1)
        hashonly = threading.Thread(target=hash_only, args=(revlist.stdout, objcheck.stdin))
        hashonly.start()
        numblobs = 0; numlarge = 1
        # Build dict with the sizes of all large blobs
        for line in objcheck.stdout:
            line = touni(line)
            objhash, blob, size = line.split()
            if blob != 'blob':
                continue
            size = int(size)
            numblobs += 1
            if size > threshsize:
                numlarge += 1
                yield objhash, size
        revlist.wait()
        objcheck.wait()
        hashonly.join()
        time1 = time.time()
        self.verbose('%d of %d blobs are >= %d bytes [elapsed %.3fs]' % (numlarge, numblobs, threshsize, time1-time0))
    def cmd_find(self, args):
        maxsize = int(args[0])
        blobsizes = dict(self.gen_large_blobs('--all', maxsize))
        time0 = time.time()
        # Find all names assumed by large blobs (those in blobsizes)
        pathsizes = collections.defaultdict(lambda:set())
        revlist = subprocess.Popen(['git', 'rev-list', '--all'], stdout=subprocess.PIPE, bufsize=-1)
        difftree = subprocess.Popen(['git', 'diff-tree', '--root', '--no-renames', '--no-commit-id', '--diff-filter=AMCR', '-r', '--stdin', '-z'],
                                    stdin=revlist.stdout, stdout=subprocess.PIPE)
        for newblob, modflag, path in difftreez_reader(difftree.stdout):
            bsize = blobsizes.get(newblob)
            if bsize:                     # We care about this blob
                pathsizes[path].add(bsize)
        time1 = time.time()
        self.verbose('Found %d paths in %.3f s' % (len(pathsizes), time1-time0))
        maxlen = max(map(len,pathsizes)) if pathsizes else 0
        for path, sizes in sorted(pathsizes.items(), key=lambda ps: max(ps[1]), reverse=True):
            print('%-*s filter=fat -text # %10d %d' % (maxlen, path,max(sizes),len(sizes)))
        revlist.wait()
        difftree.wait()
    def cmd_index_filter(self, args):
        manage_gitattributes = '--manage-gitattributes' in args
        filelist = set(f.strip() for f in open(args[0]).readlines())
        lsfiles = subprocess.Popen(['git', 'ls-files', '-s'], stdout=subprocess.PIPE)
        updateindex = subprocess.Popen(['git', 'update-index', '--index-info'], stdin=subprocess.PIPE)
        for line in lsfiles.stdout:
            line = touni(line)
            mode, sep, tail = line.partition(' ')
            blobhash, sep, tail = tail.partition(' ')
            stageno, sep, tail = tail.partition('\t')
            filename = tail.strip()
            if filename not in filelist:
                continue
            if mode == "120000":
                # skip symbolic links
                continue
            # This file will contain the hash of the cleaned object
            hashfile = os.path.join(self.gitdir, 'fat', 'index-filter', blobhash)
            try:
                cleanedobj = open(hashfile).read().rstrip()
            except IOError:
                catfile = subprocess.Popen(['git', 'cat-file', 'blob', blobhash], stdout=subprocess.PIPE)
                hashobject = subprocess.Popen(['git', 'hash-object', '-w', '--stdin'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                def dofilter():
                    self.filter_clean(catfile.stdout, hashobject.stdin)
                    hashobject.stdin.close()
                filterclean = threading.Thread(target=dofilter)
                filterclean.start()
                cleanedobj = touni(hashobject.stdout.read()).rstrip()
                catfile.wait()
                hashobject.wait()
                filterclean.join()
                mkdir_p(os.path.dirname(hashfile))
                open(hashfile, 'w').write(cleanedobj + '\n')
            updateindex.stdin.write(tobytes('%s %s %s\t%s\n' % (mode, cleanedobj, stageno, filename)))
        if manage_gitattributes:
            try:
                mode, blobsha1, stageno, filename = touni(subprocess.check_output(['git', 'ls-files', '-s', '.gitattributes'])).split()
                gitattributes_lines = touni(subprocess.check_output(['git', 'cat-file', 'blob', blobsha1])).splitlines()
            except ValueError:  # Nothing to unpack, thus no file
                mode, stageno = '100644', '0'
                gitattributes_lines = []
            gitattributes_extra = ['%s filter=fat -text' % line.split()[0] for line in filelist]
            hashobject = subprocess.Popen(['git', 'hash-object', '-w', '--stdin'], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
            stdout, stderr = hashobject.communicate(b'\n'.join(tobytes(l) for l in gitattributes_lines + gitattributes_extra) + b'\n')
            updateindex.stdin.write(tobytes('%s %s %s\t%s\n' % (mode, stdout.strip(), stageno, '.gitattributes')))
        updateindex.stdin.close()
        lsfiles.wait()
        updateindex.wait()


def main(argv):
    """ Assumes the two first argument is git-fat """
    fat = GitFat()
    cmd = argv[1] if len(argv) > 1 else ''
    if cmd == 'filter-clean':
        fat.cmd_filter_clean()
    elif cmd == 'filter-smudge':
        fat.cmd_filter_smudge()
    elif cmd == 'init':
        fat.cmd_init()
    elif cmd == 'status':
        fat.cmd_status(argv[2:])
    elif cmd == 'push':
        fat.cmd_push(argv[2:])
    elif cmd == 'pull':
        fat.cmd_pull(argv[2:])
    elif cmd == 'gc':
        fat.cmd_gc()
    elif cmd == 'verify':
        fat.cmd_verify()
    elif cmd == 'checkout':
        fat.cmd_checkout(argv[2:])
    elif cmd == 'find':
        fat.cmd_find(argv[2:])
    elif cmd == 'index-filter':
        fat.cmd_index_filter(argv[2:])
    else:
        print('Usage: git fat [init|status|push|pull|gc|verify|checkout|find|index-filter]', file=sys.stderr)
