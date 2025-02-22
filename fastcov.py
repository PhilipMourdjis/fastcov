#!/usr/bin/env python3
"""
    Author: Bryan Gillespie

    A massively parallel gcov wrapper for generating intermediate coverage formats fast

    The goal of fastcov is to generate code coverage intermediate formats as fast as possible,
    even for large projects with hundreds of gcda objects. The intermediate formats may then be
    consumed by a report generator such as lcov's genhtml, or a dedicated frontend such as coveralls.

    Sample Usage:
        $ cd build_dir
        $ ./fastcov.py --zerocounters
        $ <run unit tests>
        $ ./fastcov.py --exclude /usr/include test/ --lcov -o report.info
        $ genhtml -o code_coverage report.info
"""

import re
import os
import sys
import glob
import json
import time
import argparse
import threading
import subprocess
import multiprocessing

FASTCOV_VERSION = (1,4)
MINIMUM_PYTHON  = (3,5)
MINIMUM_GCOV    = (9,0,0)

# Interesting metrics
START_TIME = time.monotonic()
GCOVS_TOTAL = []
GCOVS_SKIPPED = []

def logger(line, quiet=True):
    if not quiet:
        print("[{:.3f}s] {}".format(stopwatch(), line))

# Global logger defaults to quiet in case developers are using as module
log = logger

def chunks(l, n):
    """Yield successive n-sized chunks from l."""
    for i in range(0, len(l), n):
        yield l[i:i + n]

def stopwatch():
    """Return number of seconds since last time this was called"""
    global START_TIME
    end_time   = time.monotonic()
    delta      = end_time - START_TIME
    START_TIME = end_time
    return delta

def parseVersionFromLine(version_str):
    """Given a string containing a dotted integer version, parse out integers and return as tuple"""
    version = re.search(r'(\d+\.\d+\.\d+)', version_str)

    if not version:
        return (0,0,0)

    return tuple(map(int, version.group(1).split(".")))

def getGcovVersion(gcov):
    p = subprocess.Popen([gcov, "-v"], stdout=subprocess.PIPE)
    output = p.communicate()[0].decode('UTF-8')
    p.wait()
    return parseVersionFromLine(output.split("\n")[0])

def removeFiles(files):
    for file in files:
        os.remove(file)

def getFilteredCoverageFiles(coverage_files, exclude):
    def excludeGcda(gcda):
        for ex in exclude:
            if ex in gcda:
                return False
        return True
    return list(filter(excludeGcda, coverage_files))

def findCoverageFiles(cwd, coverage_files, use_gcno):
    coverage_type = "user provided"
    if not coverage_files:
        coverage_type = "gcno" if use_gcno else "gcda"
        coverage_files = glob.glob(os.path.join(os.path.abspath(cwd), "**/*." + coverage_type), recursive=True)

    log("Found {} coverage files ({})".format(len(coverage_files), coverage_type))
    return coverage_files

def gcovWorker(cwd, gcov, files, chunk, gcov_filter_options, branch_coverage):
    gcov_args = "-it"
    if branch_coverage:
        gcov_args += "b"

    p = subprocess.Popen([gcov, gcov_args] + chunk, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    for line in iter(p.stdout.readline, b''):
        intermediate_json = json.loads(line.decode(sys.stdout.encoding))
        intermediate_json_files = processGcovs(cwd, intermediate_json["files"], gcov_filter_options)
        for f in intermediate_json_files:
            files.append(f) #thread safe, there might be a better way to do this though
        GCOVS_TOTAL.append(len(intermediate_json["files"]))
        GCOVS_SKIPPED.append(len(intermediate_json["files"])-len(intermediate_json_files))
    p.wait()

def processGcdas(cwd, gcov, jobs, coverage_files, gcov_filter_options, branch_coverage, min_chunk_size):
    chunk_size = max(min_chunk_size, int(len(coverage_files) / jobs) + 1)

    threads = []
    intermediate_json_files = []
    for chunk in chunks(coverage_files, chunk_size):
        t = threading.Thread(target=gcovWorker, args=(cwd, gcov, intermediate_json_files, chunk, gcov_filter_options, branch_coverage))
        threads.append(t)
        t.start()

    log("Spawned {} gcov threads, each processing at most {} coverage files".format(len(threads), chunk_size))
    for t in threads:
        t.join()

    return intermediate_json_files

def processGcov(cwd, gcov, files, gcov_filter_options):
    # Add absolute path
    gcov["file_abs"] = os.path.abspath(os.path.join(cwd, gcov["file"]))

    # If explicit sources were passed, check for match
    if gcov_filter_options["sources"]:
        if gcov["file_abs"] in gcov_filter_options["sources"]:
            files.append(gcov)
        return

    # Check include filter
    if gcov_filter_options["include"]:
        for ex in gcov_filter_options["include"]:
            if ex in gcov["file_abs"]:
                files.append(gcov)
                break
        return

    # Check exclude filter
    for ex in gcov_filter_options["exclude"]:
        if ex in gcov["file_abs"]:
            return

    files.append(gcov)

def processGcovs(cwd, gcov_files, gcov_filter_options):
    files = []
    for gcov in gcov_files:
        processGcov(cwd, gcov, files, gcov_filter_options)
    return files

def dumpBranchCoverageToLcovInfo(f, branches):
    branch_miss = 0
    branch_found = 0
    brda = []
    for line_num, branch_counts in branches.items():
        for i, count in enumerate(branch_counts):
            # Branch (<line number>, <block number>, <branch number>, <taken>)
            brda.append((line_num, int(i/2), i, count))
            branch_miss += int(count == 0)
            branch_found += 1
    for v in sorted(brda):
        f.write("BRDA:{},{},{},{}\n".format(*v))
    f.write("BRF:{}\n".format(branch_found))                # Branches Found
    f.write("BRH:{}\n".format(branch_found - branch_miss))  # Branches Hit

def dumpToLcovInfo(fastcov_json, output):
    with open(output, "w") as f:
        sources = fastcov_json["sources"]
        for sf in sorted(sources.keys()):
            data = sources[sf]
            # NOTE: TN stands for "Test Name" and appears to be unimplemented, but lcov includes it, so we do too...
            f.write("TN:\n")
            f.write("SF:{}\n".format(sf)) #Source File

            fn_miss = 0
            fn = []
            fnda = []
            for function, fdata in data["functions"].items():
                fn.append((fdata["start_line"], function))  # Function Start Line
                fnda.append((fdata["execution_count"], function))  # Function Hits
                fn_miss += int(fdata["execution_count"] == 0)
            # NOTE: lcov sorts FN, but not FNDA.
            for v in sorted(fn):
                f.write("FN:{},{}\n".format(*v))
            for v in sorted(fnda):
                f.write("FNDA:{},{}\n".format(*v))
            f.write("FNF:{}\n".format(len(data["functions"])))               #Functions Found
            f.write("FNH:{}\n".format((len(data["functions"]) - fn_miss)))   #Functions Hit

            if data["branches"]:
                dumpBranchCoverageToLcovInfo(f, data["branches"])

            line_miss = 0
            da = []
            for line_num, count in data["lines"].items():
                da.append((line_num, count))
                line_miss += int(count == 0)
            for v in sorted(da):
                f.write("DA:{},{}\n".format(*v))  # Line
            f.write("LF:{}\n".format(len(data["lines"])))                 #Lines Found
            f.write("LH:{}\n".format((len(data["lines"]) - line_miss)))   #Lines Hit
            f.write("end_of_record\n")

def getSourceLines(source, fallback_encodings=[]):
    """Return a list of lines from the provided source, trying to decode with fallback encodings if the default fails"""
    default_encoding = sys.getdefaultencoding()
    for encoding in [default_encoding] + fallback_encodings:
        try:
            with open(source, encoding=encoding) as f:
                return f.readlines()
        except UnicodeDecodeError:
            pass

    log("Warning: could not decode '{}' with {} or fallback encodings ({}); ignoring errors".format(source, default_encoding, ",".join(fallback_encodings)))
    with open(source, errors="ignore") as f:
        return f.readlines()

def exclMarkerWorker(fastcov_sources, chunk, exclude_branches_sw, include_branches_sw, fallback_encodings):
    for source in chunk:
        start_line = 0
        end_line = 0
        for i, line in enumerate(getSourceLines(source, fallback_encodings), 1): #Start enumeration at line 1
            if i in fastcov_sources[source]["branches"]:
                if include_branches_sw and all(not line.lstrip().startswith(e) for e in include_branches_sw): # Include branches starting with...
                    del fastcov_sources[source]["branches"][i]

                if exclude_branches_sw and any(line.lstrip().startswith(e) for e in exclude_branches_sw): # Exclude branches starting with...
                    del fastcov_sources[source]["branches"][i]

            if "LCOV_EXCL" not in line:
                continue

            if "LCOV_EXCL_LINE" in line:
                for key in ["lines", "branches"]:
                    if i in fastcov_sources[source][key]:
                        del fastcov_sources[source][key][i]
            elif "LCOV_EXCL_START" in line:
                start_line = i
            elif "LCOV_EXCL_STOP" in line:
                end_line = i

                if not start_line:
                    end_line = 0
                    continue

                for key in ["lines", "branches"]:
                    for line_num in list(fastcov_sources[source][key].keys()):
                        if start_line <= line_num <= end_line:
                            del fastcov_sources[source][key][line_num]

                start_line = end_line = 0
            elif "LCOV_EXCL_BR_LINE" in line:
                if i in fastcov_sources[source]["branches"]:
                    del fastcov_sources[source]["branches"][i]

def scanExclusionMarkers(fastcov_json, jobs, exclude_branches_sw, include_branches_sw, min_chunk_size, fallback_encodings):
    chunk_size = max(min_chunk_size, int(len(fastcov_json["sources"]) / jobs) + 1)

    threads = []
    for chunk in chunks(list(fastcov_json["sources"].keys()), chunk_size):
        t = threading.Thread(target=exclMarkerWorker, args=(fastcov_json["sources"], chunk, exclude_branches_sw, include_branches_sw, fallback_encodings))
        threads.append(t)
        t.start()

    log("Spawned {} threads each scanning at most {} source files".format(len(threads), chunk_size))
    for t in threads:
        t.join()

def distillFunction(function_raw, functions):
    function_name   = function_raw["name"]
    # NOTE: need to explicitly cast all counts coming from gcov to int - this is because gcov's json library
    # will pass as scientific notation (i.e. 12+e45)
    start_line      = int(function_raw["start_line"])
    execution_count = int(function_raw["execution_count"])
    if function_name not in functions:
        functions[function_name] = {
            "start_line": start_line,
            "execution_count": execution_count
        }
    else:
        functions[function_name]["execution_count"] += execution_count

def emptyBranchSet(branch1, branch2):
    return (branch1["count"] == 0 and branch2["count"] == 0)

def matchingBranchSet(branch1, branch2):
    return (branch1["count"] == branch2["count"])

def filterExceptionalBranches(branches):
    filtered_branches = []
    exception_branch = False
    for i in range(0, len(branches), 2):
        if i+1 >= len(branches):
            filtered_branches.append(branches[i])
            break

        # Filter exceptional branch noise
        if branches[i+1]["throw"]:
            exception_branch = True
            continue

        # Filter initializer list noise
        if exception_branch and emptyBranchSet(branches[i], branches[i+1]) and len(filtered_branches) >= 2 and matchingBranchSet(filtered_branches[-1], filtered_branches[-2]):
            return []

        filtered_branches.append(branches[i])
        filtered_branches.append(branches[i+1])

    return filtered_branches

def distillLine(line_raw, lines, branches, include_exceptional_branches):
    line_number = int(line_raw["line_number"])
    count       = int(line_raw["count"])
    if line_number not in lines:
        lines[line_number] = count
    else:
        lines[line_number] += count

    # Filter out exceptional branches by default unless requested otherwise
    if not include_exceptional_branches:
        line_raw["branches"] = filterExceptionalBranches(line_raw["branches"])

    # Increment all branch counts
    for i, branch in enumerate(line_raw["branches"]):
        if line_number not in branches:
            branches[line_number] = []
        blen = len(branches[line_number])
        glen = len(line_raw["branches"])
        if blen < glen:
            branches[line_number] += [0] * (glen - blen)
        branches[line_number][i] += int(branch["count"])

def distillSource(source_raw, sources, include_exceptional_branches):
    source_name = source_raw["file_abs"]
    if source_name not in sources:
        sources[source_name] = {
            "functions": {},
            "branches": {},
            "lines": {},
        }

    for function in source_raw["functions"]:
        distillFunction(function, sources[source_name]["functions"])

    for line in source_raw["lines"]:
        distillLine(line, sources[source_name]["lines"], sources[source_name]["branches"], include_exceptional_branches)

def distillReport(report_raw, include_exceptional_branches):
    report_json = {
        "sources": {}
    }

    for source in report_raw:
        distillSource(source, report_json["sources"], include_exceptional_branches)

    return report_json

def dumpToJson(intermediate, output):
    with open(output, "w") as f:
        json.dump(intermediate, f)

def getGcovFilterOptions(args):
    return {
        "sources": set([os.path.abspath(s) for s in args.sources]), #Make paths absolute, use set for fast lookups
        "include": args.includepost,
        "exclude": args.excludepost,
    }

def tupleToDotted(tup):
    return ".".join(map(str, tup))

def parseArgs():
    parser = argparse.ArgumentParser(description='A parallel gcov wrapper for fast coverage report generation')
    parser.add_argument('-z', '--zerocounters', dest='zerocounters', action="store_true", help='Recursively delete all gcda files')

    # Enable Branch Coverage
    parser.add_argument('-b', '--branch-coverage', dest='branchcoverage', action="store_true", help='Include only the most useful branches in the coverage report.')
    parser.add_argument('-B', '--exceptional-branch-coverage', dest='xbranchcoverage', action="store_true", help='Include ALL branches in the coverage report (including potentially noisy exceptional branches).')
    parser.add_argument('-A', '--exclude-br-lines-starting-with', dest='exclude_branches_sw', nargs="+", metavar='', default=[], help='Exclude branches from lines starting with one of the provided strings (i.e. assert, return, etc.)')
    parser.add_argument('-a', '--include-br-lines-starting-with', dest='include_branches_sw', nargs="+", metavar='', default=[], help='Include only branches from lines starting with one of the provided strings (i.e. if, else, while, etc.)')

    # Capture untested file coverage as well via gcno
    parser.add_argument('-n', '--process-gcno', dest='use_gcno', action="store_true", help='Process both gcno and gcda coverage files. This option is useful for capturing untested files in the coverage report.')

    # Filtering Options
    parser.add_argument('-s', '--source-files', dest='sources',     nargs="+", metavar='', default=[], help='Filter: Specify exactly which source files should be included in the final report. Paths must be either absolute or relative to current directory.')
    parser.add_argument('-e', '--exclude',      dest='excludepost', nargs="+", metavar='', default=[], help='Filter: Exclude source files from final report if they contain one of the provided substrings (i.e. /usr/include test/, etc.)')
    parser.add_argument('-i', '--include',      dest='includepost', nargs="+", metavar='', default=[], help='Filter: Only include source files in final report that contain one of the provided substrings (i.e. src/ etc.)')
    parser.add_argument('-f', '--gcda-files',   dest='coverage_files',  nargs="+", metavar='', default=[], help='Filter: Specify exactly which gcda or gcno files should be processed. Note that specifying gcno causes both gcno and gcda to be processed.')
    parser.add_argument('-E', '--exclude-gcda', dest='excludepre',  nargs="+", metavar='', default=[], help='Filter: Exclude gcda or gcno files from being processed via simple find matching (not regex)')

    parser.add_argument('-g', '--gcov', dest='gcov', default='gcov', help='Which gcov binary to use')

    parser.add_argument('-d', '--search-directory', dest='directory', default=".", help='Base directory to recursively search for gcda files (default: .)')
    parser.add_argument('-c', '--compiler-directory', dest='cdirectory', default=".", help='Base directory compiler was invoked from (default: .) \
                                                                                            This needs to be set if invoking fastcov from somewhere other than the base compiler directory.')

    parser.add_argument('-j', '--jobs', dest='jobs', type=int, default=multiprocessing.cpu_count(), help='Number of parallel gcov to spawn (default: {}).'.format(multiprocessing.cpu_count()))
    parser.add_argument('-m', '--minimum-chunk-size', dest='minimum_chunk', type=int, default=5, help='Minimum number of files a thread should process (default: 5). \
                                                                                                       If you have only 4 gcda files but they are monstrously huge, you could change this value to a 1 so that each thread will only process 1 gcda. Otherwise fastcov will spawn only 1 thread to process all of them.')

    parser.add_argument('-F', '--fallback-encodings', dest='fallback_encodings', nargs="+", metavar='', default=[], help='List of encodings to try if opening a source file with the default fails (i.e. latin1, etc.). This option is not usually needed.')

    parser.add_argument('-l', '--lcov',     dest='lcov',     action="store_true", help='Output in lcov info format instead of fastcov json')
    parser.add_argument('-r', '--gcov-raw', dest='gcov_raw', action="store_true", help='Output in gcov raw json instead of fastcov json')
    parser.add_argument('-o', '--output',  dest='output', default="coverage.json", help='Name of output file (default: coverage.json)')
    parser.add_argument('-q', '--quiet', dest='quiet', action="store_true", help='Suppress output to stdout')

    parser.add_argument('-v', '--version', action="version", version='%(prog)s {version}'.format(version=__version__), help="Show program's version number and exit")

    args = parser.parse_args()

    def arg_logger(line):
        logger(line, quiet=args.quiet)

    # Change global logger settings to reflect arguments
    global log
    log = arg_logger

    return args

def checkPythonVersion(version):
    """Exit if the provided python version is less than the supported version"""
    if version < MINIMUM_PYTHON:
        sys.stderr.write("Minimum python version {} required, found {}\n".format(tupleToDotted(MINIMUM_PYTHON), tupleToDotted(version)))
        sys.exit(1)

def checkGcovVersion(version):
    """Exit if the provided gcov version is less than the supported version"""
    if version < MINIMUM_GCOV:
        sys.stderr.write("Minimum gcov version {} required, found {}\n".format(tupleToDotted(MINIMUM_GCOV), tupleToDotted(version)))
        sys.exit(2)

def main():
    args = parseArgs()

    # Need at least python 3.5 because of use of recursive glob
    checkPythonVersion(sys.version_info[0:2])

    # Need at least gcov 9.0.0 because that's when gcov JSON and stdout streaming was introduced
    checkGcovVersion(getGcovVersion(args.gcov))

    # Get list of gcda files to process
    coverage_files = findCoverageFiles(args.directory, args.coverage_files, args.use_gcno)

    # If gcda/gcno filtering is enabled, filter them out now
    if args.excludepre:
        coverage_files = getFilteredCoverageFiles(coverage_files, args.excludepre)
        log("Found {} coverage files after filtering".format(len(coverage_files)))

    # We "zero" the "counters" by simply deleting all gcda files
    if args.zerocounters:
        removeFiles(coverage_files)
        log("Removed {} .gcda files".format(len(coverage_files)))
        return

    # Fire up one gcov per cpu and start processing gcdas
    gcov_filter_options = getGcovFilterOptions(args)
    intermediate_json_files = processGcdas(args.cdirectory, args.gcov, args.jobs, coverage_files, gcov_filter_options, args.branchcoverage or args.xbranchcoverage, args.minimum_chunk)

    # Summarize processing results
    gcov_total = sum(GCOVS_TOTAL)
    gcov_skipped = sum(GCOVS_SKIPPED)
    log("Processed {} .gcov files ({} total, {} skipped)".format(gcov_total - gcov_skipped, gcov_total, gcov_skipped))

    # Distill all the extraneous info gcov gives us down to the core report
    fastcov_json = distillReport(intermediate_json_files, args.xbranchcoverage)
    log("Aggregated raw gcov JSON into fastcov JSON report")

    # Scan for exclusion markers
    scanExclusionMarkers(fastcov_json, args.jobs, args.exclude_branches_sw, args.include_branches_sw, args.minimum_chunk, args.fallback_encodings)
    log("Scanned {} source files for exclusion markers".format(len(fastcov_json["sources"])))

    # Dump to desired file format
    if args.lcov:
        dumpToLcovInfo(fastcov_json, args.output)
        log("Created lcov info file '{}'".format(args.output))
    elif args.gcov_raw:
        dumpToJson(intermediate_json_files, args.output)
        log("Created gcov raw json file '{}'".format(args.output))
    else:
        dumpToJson(fastcov_json, args.output)
        log("Created fastcov json file '{}'".format(args.output))

# Set package version... it's way down here so that we can call tupleToDotted
__version__ = tupleToDotted(FASTCOV_VERSION)

if __name__ == '__main__':
    main()
